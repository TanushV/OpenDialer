use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{Manager, State};
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};
use uuid::Uuid;

/// Exit status of the Python authority when another authority owns its data directory.
const AUTHORITY_BUSY_EXIT: i32 = 75;

struct Backend {
    config: BackendConfig,
    child: CommandChild,
    generation: u64,
    /// Set by the event drain once the owned child has terminated.
    exited: Arc<AtomicBool>,
}
#[derive(Clone, Serialize)]
struct BackendConfig {
    url: String,
    local_token: String,
    simulation: bool,
}
#[derive(Default)]
struct LocalRuntime {
    backend: Option<Backend>,
    generation: u64,
    /// Mode whose last authority refused to start because another authority owns its data.
    refused: Option<bool>,
}
#[derive(Default, Clone)]
struct LocalBackend(Arc<Mutex<LocalRuntime>>);

impl LocalBackend {
    /// The running managed authority; a terminated child is never offered for reuse.
    fn current(&self) -> Result<Option<BackendConfig>, String> {
        Ok(self
            .0
            .lock()
            .map_err(|_| "Local authority unavailable")?
            .backend
            .as_ref()
            .filter(|b| !b.exited.load(Ordering::SeqCst))
            .map(|b| b.config.clone()))
    }
    fn take(&self) -> Result<Option<Backend>, String> {
        Ok(self
            .0
            .lock()
            .map_err(|_| "Local authority unavailable")?
            .backend
            .take())
    }
    /// Remove only the instance whose event stream ended, recording a lock refusal.
    fn forget_exited(&self, generation: u64, code: Option<i32>) -> Option<Backend> {
        let mut state = self.0.lock().ok()?;
        if state
            .backend
            .as_ref()
            .is_none_or(|b| b.generation != generation)
        {
            return None;
        }
        let backend = state.backend.take()?;
        if code == Some(AUTHORITY_BUSY_EXIT) {
            state.refused = Some(backend.config.simulation);
        }
        Some(backend)
    }
}

#[derive(Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct WebhookTunnelConfig {
    enabled: bool,
    host: String,
    user: String,
    remote_port: u16,
}

fn validate_tunnel(config: &WebhookTunnelConfig) -> Result<(), String> {
    if !config.enabled && config.host.is_empty() && config.user.is_empty() {
        return Ok(());
    }
    let host_ok = !config.host.is_empty()
        && config.host.len() <= 253
        && config.host.split('.').all(|label| {
            !label.is_empty()
                && label.len() <= 63
                && label.as_bytes()[0].is_ascii_alphanumeric()
                && label.as_bytes()[label.len() - 1].is_ascii_alphanumeric()
                && label
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || c == b'-')
        });
    let user_ok = !config.user.is_empty()
        && config.user.len() <= 64
        && config.user.as_bytes()[0].is_ascii_alphanumeric()
        && config
            .user
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c));
    if !host_ok || !user_ok || config.remote_port < 1024 {
        return Err("Use a hostname, SSH user and dedicated remote port 1024–65535".into());
    }
    Ok(())
}

fn webhook_ssh_args(config: &WebhookTunnelConfig, runtime: &str) -> Result<Vec<String>, String> {
    validate_tunnel(config)?;
    let url = url::Url::parse(runtime).map_err(|_| "Invalid managed local destination")?;
    if !config.enabled
        || url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || url.port().is_none()
        || url.path() != "/"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err("Forwarding requires the current managed loopback authority".into());
    }
    Ok(vec![
        "-NT".into(),
        "-o".into(),
        "BatchMode=yes".into(),
        "-o".into(),
        "StrictHostKeyChecking=yes".into(),
        "-o".into(),
        "ExitOnForwardFailure=yes".into(),
        "-o".into(),
        "ServerAliveInterval=30".into(),
        "-o".into(),
        "ServerAliveCountMax=3".into(),
        // Never join or create a user-configured multiplexing master: a shared master could
        // keep this forward alive after the owned child exits.
        "-o".into(),
        "ControlMaster=no".into(),
        "-o".into(),
        "ControlPath=none".into(),
        "-R".into(),
        format!(
            "127.0.0.1:{}:127.0.0.1:{}",
            config.remote_port,
            url.port().unwrap()
        ),
        format!("{}@{}", config.user, config.host),
    ])
}

fn tunnel_retry_delay(attempt: usize) -> Duration {
    Duration::from_secs([1, 2, 4, 8, 16, 30][attempt.min(5)])
}

#[derive(Debug)]
struct OwnedWebhookTunnel {
    child: Option<std::process::Child>,
    /// The child leads its own Unix process group, which is terminated as a unit.
    group: bool,
}
impl OwnedWebhookTunnel {
    fn new_group(child: std::process::Child) -> Self {
        Self {
            child: Some(child),
            group: cfg!(unix),
        }
    }
    fn stop(&mut self) -> Result<(), String> {
        if let Some(child) = self.child.as_mut() {
            #[cfg(unix)]
            if self.group {
                // Also stops ProxyCommand/ProxyJump helpers. Best effort: ESRCH means every
                // member already exited; the leader is still killed and reaped below.
                // SAFETY: killpg only sends a signal to the group this tunnel created.
                let _ = unsafe { libc::killpg(child.id() as libc::pid_t, libc::SIGKILL) };
            }
            #[cfg(not(unix))]
            let _ = self.group;
            if child
                .try_wait()
                .map_err(|_| "Cannot inspect owned SSH child")?
                .is_none()
            {
                child.kill().map_err(|_| "Cannot stop owned SSH child")?;
            }
            child.wait().map_err(|_| "Cannot reap owned SSH child")?;
            self.child = None;
        }
        Ok(())
    }
}
impl Drop for OwnedWebhookTunnel {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

fn spawn_webhook_ssh(program: &str, args: &[String]) -> Result<OwnedWebhookTunnel, String> {
    let mut command = std::process::Command::new(program);
    command
        .args(args)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    command
        .spawn()
        .map(OwnedWebhookTunnel::new_group)
        .map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                "ssh_missing"
            } else {
                "ssh_start_failed"
            }
            .into()
        })
}

struct TunnelSupervisor {
    stop: std::sync::mpsc::Sender<()>,
    thread: Option<std::thread::JoinHandle<Result<(), String>>>,
    status: Arc<Mutex<String>>,
}
impl TunnelSupervisor {
    fn start(
        mut spawn: impl FnMut() -> Result<OwnedWebhookTunnel, String> + Send + 'static,
    ) -> Self {
        let (tx, rx) = std::sync::mpsc::channel();
        let status = Arc::new(Mutex::new("starting".to_string()));
        let reported = status.clone();
        let thread = std::thread::spawn(move || {
            let mut failures = 0;
            loop {
                if rx.try_recv().is_ok() {
                    return Ok(());
                }
                match spawn() {
                    Ok(mut owned) => {
                        *reported.lock().map_err(|_| "Tunnel status unavailable")? =
                            "running_unverified".into();
                        let started = std::time::Instant::now();
                        loop {
                            if rx.recv_timeout(Duration::from_millis(100)).is_ok() {
                                return owned.stop();
                            }
                            if owned
                                .child
                                .as_mut()
                                .unwrap()
                                .try_wait()
                                .map_err(|_| "Cannot inspect SSH child")?
                                .is_some()
                            {
                                owned.stop()?;
                                break;
                            }
                        }
                        if started.elapsed() >= Duration::from_secs(60) {
                            failures = 0;
                        }
                        *reported.lock().map_err(|_| "Tunnel status unavailable")? =
                            "host_or_forward_unavailable".into();
                    }
                    Err(reason) => {
                        *reported.lock().map_err(|_| "Tunnel status unavailable")? = reason
                    }
                }
                if rx.recv_timeout(tunnel_retry_delay(failures)).is_ok() {
                    return Ok(());
                }
                failures = failures.saturating_add(1);
            }
        });
        Self {
            stop: tx,
            thread: Some(thread),
            status,
        }
    }
    fn stop(&mut self) -> Result<(), String> {
        let _ = self.stop.send(());
        if let Some(thread) = self.thread.take() {
            thread
                .join()
                .map_err(|_| "Tunnel supervisor interrupted")??;
        }
        Ok(())
    }
}
impl Drop for TunnelSupervisor {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

#[derive(Default, Clone)]
struct WebhookTunnel(Arc<Mutex<Option<(String, TunnelSupervisor)>>>);
impl WebhookTunnel {
    fn stop(&self) -> Result<(), String> {
        if let Some((_, mut task)) = self.0.lock().map_err(|_| "Tunnel unavailable")?.take() {
            task.stop()?;
        }
        Ok(())
    }
    /// Stop the forward only if it still targets the given (exited) local authority.
    fn stop_for(&self, runtime: &str) -> Result<(), String> {
        let task = {
            let mut owned = self.0.lock().map_err(|_| "Tunnel unavailable")?;
            if owned.as_ref().is_some_and(|(url, _)| url == runtime) {
                owned.take()
            } else {
                None
            }
        };
        if let Some((_, mut task)) = task {
            task.stop()?;
        }
        Ok(())
    }
}

fn load_tunnel_config(path: &std::path::Path) -> Result<WebhookTunnelConfig, String> {
    if !path.exists() {
        return Ok(WebhookTunnelConfig {
            remote_port: 18765,
            ..Default::default()
        });
    }
    if std::fs::metadata(path)
        .map_err(|_| "Cannot read forwarding configuration")?
        .len()
        > 4096
    {
        return Err("Forwarding configuration exceeds its bound".into());
    }
    let config = serde_json::from_slice(
        &std::fs::read(path).map_err(|_| "Cannot read forwarding configuration")?,
    )
    .map_err(|_| "Invalid forwarding configuration")?;
    validate_tunnel(&config)?;
    Ok(config)
}

fn save_tunnel_config(path: &std::path::Path, config: &WebhookTunnelConfig) -> Result<(), String> {
    validate_tunnel(config)?;
    let parent = path.parent().ok_or("Invalid forwarding location")?;
    std::fs::create_dir_all(parent).map_err(|_| "Cannot create forwarding configuration")?;
    write_private_file(
        path,
        &serde_json::to_vec(config).map_err(|_| "Invalid configuration")?,
    )
}

/// Replace a small settings file atomically: write a temporary file, then rename it.
fn write_private_file(path: &std::path::Path, bytes: &[u8]) -> Result<(), String> {
    let temp = path.with_extension(format!("{}.tmp", Uuid::new_v4()));
    std::fs::write(&temp, bytes).map_err(|_| "Cannot write a settings file")?;
    std::fs::rename(&temp, path).map_err(|_| {
        let _ = std::fs::remove_file(&temp);
        "Cannot publish a settings file".to_string()
    })
}

fn local_backend_healthy(runtime: &str) -> bool {
    use std::io::{Read, Write};
    let Ok(url) = url::Url::parse(runtime) else {
        return false;
    };
    let Some(port) = url.port() else {
        return false;
    };
    let timeout = Duration::from_millis(200);
    let Ok(mut stream) = std::net::TcpStream::connect_timeout(
        &std::net::SocketAddr::from(([127, 0, 0, 1], port)),
        timeout,
    ) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(timeout));
    let _ = stream.set_write_timeout(Some(timeout));
    if stream
        .write_all(b"GET /health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        .is_err()
    {
        return false;
    }
    let mut result = [0; 32];
    matches!(stream.read(&mut result), Ok(n) if n >= 12 && &result[9..12] == b"200")
}

fn sync_tunnel(app: &tauri::AppHandle, runtime: &BackendConfig) -> Result<(), String> {
    let state = app.state::<WebhookTunnel>();
    if runtime.simulation {
        return state.stop();
    }
    let config = load_tunnel_config(
        &app.path()
            .app_data_dir()
            .map_err(|_| "App data unavailable")?
            .join("webhook-forward.json"),
    )?;
    if !config.enabled {
        return state.stop();
    }
    let args = webhook_ssh_args(&config, &runtime.url)?;
    let mut task = state.0.lock().map_err(|_| "Tunnel unavailable")?;
    if task.as_ref().is_some_and(|(url, _)| url == &runtime.url) {
        return Ok(());
    }
    if let Some((_, mut previous)) = task.take() {
        previous.stop()?;
    }
    let url = runtime.url.clone();
    #[cfg(target_os = "macos")]
    let program = "/usr/bin/ssh".to_string();
    #[cfg(windows)]
    let program = std::path::PathBuf::from(
        std::env::var_os("SystemRoot").ok_or("Windows system directory unavailable")?,
    )
    .join("System32/OpenSSH/ssh.exe")
    .to_string_lossy()
    .into_owned();
    #[cfg(not(any(target_os = "macos", windows)))]
    let program = "ssh".to_string();
    let supervisor = TunnelSupervisor::start(move || {
        if !local_backend_healthy(&url) {
            return Err("waiting_for_local_authority".into());
        }
        spawn_webhook_ssh(&program, &args)
    });
    *task = Some((runtime.url.clone(), supervisor));
    Ok(())
}

#[derive(Serialize)]
struct TunnelView {
    config: WebhookTunnelConfig,
    state: String,
}

#[tauri::command]
async fn webhook_tunnel_status(app: tauri::AppHandle) -> Result<TunnelView, String> {
    blocking(move || {
        let config = load_tunnel_config(
            &app.path()
                .app_data_dir()
                .map_err(|_| "App data unavailable")?
                .join("webhook-forward.json"),
        )?;
        let tunnel = app.state::<WebhookTunnel>();
        let owned = tunnel.0.lock().map_err(|_| "Tunnel unavailable")?;
        let state = if let Some((_, task)) = owned.as_ref() {
            task.status
                .lock()
                .map_err(|_| "Tunnel status unavailable")?
                .clone()
        } else if config.enabled {
            "awaiting_local_authority".into()
        } else {
            "disabled".into()
        };
        Ok(TunnelView { config, state })
    })
    .await
}

#[tauri::command]
async fn webhook_tunnel_set(
    app: tauri::AppHandle,
    config: WebhookTunnelConfig,
) -> Result<(), String> {
    blocking(move || {
        validate_tunnel(&config)?;
        let runtime = app.state::<LocalBackend>().current()?;
        if runtime.as_ref().is_some_and(|r| r.simulation) {
            return Err("Forwarding is available only for the local workspace".into());
        }
        save_tunnel_config(
            &app.path()
                .app_data_dir()
                .map_err(|_| "App data unavailable")?
                .join("webhook-forward.json"),
            &config,
        )?;
        app.state::<WebhookTunnel>().stop()?;
        if let Some(runtime) = runtime {
            sync_tunnel(&app, &runtime)?;
        }
        Ok(())
    })
    .await
}

struct ExitCleanup(Mutex<Option<Box<dyn FnOnce() + Send>>>);
impl tauri::Resource for ExitCleanup {}
impl Drop for ExitCleanup {
    fn drop(&mut self) {
        if let Ok(callback) = self.0.get_mut() {
            if let Some(callback) = callback.take() {
                callback();
            }
        }
    }
}

/// Updater cleanup clears resources before process::exit, without a RunEvent::Exit.
pub fn register_exit_cleanup<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    callback: impl FnOnce() + Send + 'static,
) {
    app.resources_table()
        .add(ExitCleanup(Mutex::new(Some(Box::new(callback)))));
}

pub fn managed_sidecar(
    command: tauri_plugin_shell::process::Command,
) -> tauri_plugin_shell::process::Command {
    command.set_process_group(true)
}

pub async fn blocking<T: Send + 'static, F: FnOnce() -> Result<T, String> + Send + 'static>(
    operation: F,
) -> Result<T, String> {
    tauri::async_runtime::spawn_blocking(operation)
        .await
        .map_err(|_| "Desktop operation was interrupted".to_string())?
}

#[tauri::command]
async fn backend_config(
    app: tauri::AppHandle,
    state: State<'_, LocalBackend>,
    simulation: bool,
) -> Result<BackendConfig, String> {
    let backend = state.inner().clone();
    blocking(move || {
        let runtime = start_backend(&app, &backend, simulation)?;
        sync_tunnel(&app, &runtime)?;
        Ok(runtime)
    })
    .await
}

fn start_backend(
    app: &tauri::AppHandle,
    state: &LocalBackend,
    simulation: bool,
) -> Result<BackendConfig, String> {
    if let Some(existing) = state.current()?.filter(|c| c.simulation == simulation) {
        return Ok(existing);
    }
    // Store format 2: 0.2/0.3 kept encrypted stores in `local` and `simulation`, which this
    // authority cannot read. New folders start fresh and leave that data untouched.
    let directory = app
        .path()
        .app_data_dir()
        .map_err(|_| "App data directory unavailable")?
        .join(if simulation {
            "simulation-2"
        } else {
            "local-2"
        });
    std::fs::create_dir_all(&directory).map_err(|_| "Cannot create private application data")?;
    let mut locked = state
        .0
        .lock()
        .map_err(|_| "Local service state unavailable")?;
    if let Some(existing) = locked.backend.as_ref() {
        if existing.config.simulation == simulation && !existing.exited.load(Ordering::SeqCst) {
            return Ok(existing.config.clone());
        }
    }
    if locked.refused.take() == Some(simulation) {
        return Err("Another OpenDialer authority is already using this workspace's data. Quit the other OpenDialer window or process, then retry.".into());
    }
    if let Some(old) = locked.backend.take() {
        app.state::<WebhookTunnel>().stop()?;
        old.child
            .kill()
            .map_err(|_| "Cannot stop the previous local service")?;
    }
    let listener =
        std::net::TcpListener::bind("127.0.0.1:0").map_err(|_| "No loopback port is available")?;
    let port = listener
        .local_addr()
        .map_err(|_| "No local address")?
        .port();
    drop(listener);
    let nonce = format!("{}{}", Uuid::new_v4(), Uuid::new_v4());
    let mut args = vec![
        "serve".to_string(),
        "--host".into(),
        "127.0.0.1".into(),
        "--port".into(),
        port.to_string(),
        "--mode".into(),
        "local".into(),
        "--data-dir".into(),
        directory.to_string_lossy().to_string(),
        "--secrets-stdin".into(),
    ];
    if simulation {
        args.push("--simulation".into());
    }
    // The local token travels over the child's private stdin pipe, never through its environment.
    // The authority treats end-of-file on that pipe as loss of this launcher.
    let command = managed_sidecar(
        app.shell()
            .sidecar("opendialer-backend")
            .map_err(|_| "Bundled local service is missing")?,
    )
    .args(args)
    .env("OMP_NUM_THREADS", "1")
    .env("OPENBLAS_NUM_THREADS", "1");
    let (receiver, mut child) = command
        .spawn()
        .map_err(|_| "Cannot start the bundled local service")?;
    if child.write(&launch_secrets(&nonce)?).is_err() {
        let _ = child.kill();
        return Err("Cannot hand secrets to the bundled local service".into());
    }
    locked.generation += 1;
    let generation = locked.generation;
    let exited = Arc::new(AtomicBool::new(false));
    let config = BackendConfig {
        url: format!("http://127.0.0.1:{port}"),
        local_token: nonce,
        simulation,
    };
    tauri::async_runtime::spawn(drain_backend(
        receiver,
        state.clone(),
        app.state::<WebhookTunnel>().inner().clone(),
        generation,
        exited.clone(),
    ));
    locked.backend = Some(Backend {
        config: config.clone(),
        child,
        generation,
        exited,
    });
    drop(locked);
    Ok(config)
}

/// One JSON line, read once by the authority (`--secrets-stdin`).
fn launch_secrets(local_token: &str) -> Result<Vec<u8>, String> {
    let mut line = serde_json::to_vec(&serde_json::json!({ "local_token": local_token }))
        .map_err(|_| "Cannot prepare the local service token")?;
    line.push(b'\n');
    Ok(line)
}

/// Drain without forwarding provider or user content into ordinary desktop logs. When the
/// owned child exits, forget it so the next request starts a fresh authority.
async fn drain_backend(
    mut events: tauri::async_runtime::Receiver<CommandEvent>,
    local: LocalBackend,
    tunnel: WebhookTunnel,
    generation: u64,
    exited: Arc<AtomicBool>,
) {
    let mut code = None;
    while let Some(event) = events.recv().await {
        if let CommandEvent::Terminated(payload) = event {
            code = payload.code;
        }
    }
    exited.store(true, Ordering::SeqCst);
    let _ = tauri::async_runtime::spawn_blocking(move || {
        if let Some(backend) = local.forget_exited(generation, code) {
            let url = backend.config.url.clone();
            // Reap any remaining group/job members; the leader has already exited.
            let _ = backend.child.kill();
            let _ = tunnel.stop_for(&url);
        }
    })
    .await;
}

#[tauri::command]
fn open_source(app: tauri::AppHandle, value: String) -> Result<(), String> {
    use tauri_plugin_opener::OpenerExt;
    let parsed = url::Url::parse(&value).map_err(|_| "Invalid source URL")?;
    if parsed.scheme() != "https" || !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("Source links must be HTTPS".into());
    }
    app.opener()
        .open_url(value, None::<&str>)
        .map_err(|_| "Cannot open source link".into())
}

/// Quit, mode change and updater cleanup: stop the owned SSH child and wait for it, then
/// terminate the authority's process group (Unix) or job object (Windows) and wait for it.
fn stop_owned_runtimes(tunnel: &WebhookTunnel, backend: &LocalBackend) {
    let _ = tunnel.stop();
    if let Ok(Some(owned)) = backend.take() {
        let _ = owned.child.kill();
        #[cfg(windows)]
        wait_for_backend_release(Duration::from_secs(10));
    }
}

/// The updater's installer replaces opendialer-backend.exe and opendialer-trainer.exe right after
/// cleanup. A training child is the authority's job member, so the job kill also ends it, but a
/// member that is still terminating keeps its image mapped: wait until each executable can be
/// opened exclusively, within one shared limit.
#[cfg(windows)]
fn wait_for_backend_release(limit: Duration) {
    use std::os::windows::fs::OpenOptionsExt;
    let Some(directory) = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(std::path::Path::to_path_buf))
    else {
        return;
    };
    let deadline = std::time::Instant::now() + limit;
    for name in ["opendialer-backend.exe", "opendialer-trainer.exe"] {
        let path = directory.join(name);
        while path.is_file() && std::time::Instant::now() < deadline {
            match std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .share_mode(0)
                .open(&path)
            {
                Ok(_) => break,
                // ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION while a process still uses it.
                Err(error) if matches!(error.raw_os_error(), Some(32 | 33)) => {
                    std::thread::sleep(Duration::from_millis(100))
                }
                Err(_) => break,
            }
        }
    }
}

pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_opener::init())
        .manage(LocalBackend::default())
        .manage(WebhookTunnel::default())
        .setup(|app| {
            // The updater clears the resource table before it launches an installer and exits,
            // which runs this guard.
            let backend = app.state::<LocalBackend>().inner().clone();
            let tunnel = app.state::<WebhookTunnel>().inner().clone();
            register_exit_cleanup(app.handle(), move || stop_owned_runtimes(&tunnel, &backend));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            backend_config,
            open_source,
            webhook_tunnel_status,
            webhook_tunnel_set
        ])
        .build(tauri::generate_context!())
        .expect("OpenDialer could not start");
    app.run(|handle, event| {
        if let tauri::RunEvent::Exit = event {
            stop_owned_runtimes(
                &handle.state::<WebhookTunnel>(),
                &handle.state::<LocalBackend>(),
            );
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_root(label: &str) -> std::path::PathBuf {
        let root = std::env::temp_dir().join(format!("opendialer-{label}-{}", Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        root
    }

    fn fixture() -> WebhookTunnelConfig {
        WebhookTunnelConfig {
            enabled: true,
            host: "forwarder.example.test".into(),
            user: "dialer".into(),
            remote_port: 18765,
        }
    }

    #[test]
    fn webhook_tunnel_owned_runtime_lifecycle() {
        let config = fixture();
        for port in [34001, 59123] {
            let args = webhook_ssh_args(&config, &format!("http://127.0.0.1:{port}")).unwrap();
            assert_eq!(
                args,
                vec![
                    "-NT".to_string(),
                    "-o".into(),
                    "BatchMode=yes".into(),
                    "-o".into(),
                    "StrictHostKeyChecking=yes".into(),
                    "-o".into(),
                    "ExitOnForwardFailure=yes".into(),
                    "-o".into(),
                    "ServerAliveInterval=30".into(),
                    "-o".into(),
                    "ServerAliveCountMax=3".into(),
                    "-o".into(),
                    "ControlMaster=no".into(),
                    "-o".into(),
                    "ControlPath=none".into(),
                    "-R".into(),
                    format!("127.0.0.1:18765:127.0.0.1:{port}"),
                    "dialer@forwarder.example.test".into(),
                ]
            );
        }
        let saved = serde_json::to_string(&config).unwrap();
        assert!(!saved.contains("local_token") && !saved.contains("34001"));
        let waits: Vec<_> = (0..9).map(tunnel_retry_delay).collect();
        assert_eq!(
            waits,
            [1, 2, 4, 8, 16, 30, 30, 30, 30].map(Duration::from_secs)
        );
    }

    #[test]
    fn webhook_tunnel_rejects_argument_injection_and_nonmanaged_destination() {
        for host in [
            "-oProxyCommand=bad",
            "example.test;echo",
            "example.test\nHost *",
            "user@host",
            "host name",
        ] {
            let mut config = fixture();
            config.host = host.into();
            assert!(webhook_ssh_args(&config, "http://127.0.0.1:34001").is_err());
        }
        for user in ["-root", "root;echo", "user name", "user@host", "user\n"] {
            let mut config = fixture();
            config.user = user.into();
            assert!(webhook_ssh_args(&config, "http://127.0.0.1:34001").is_err());
        }
        for url in [
            "https://remote.example.test",
            "http://127.0.0.1:34001/v1",
            "http://user@127.0.0.1:34001",
            "http://127.0.0.1:34001?token=secret",
            "http://127.0.0.1",
        ] {
            assert!(webhook_ssh_args(&fixture(), url).is_err());
        }
        let mut disabled = fixture();
        disabled.enabled = false;
        assert!(webhook_ssh_args(&disabled, "http://127.0.0.1:34001").is_err());
        // A user ssh_config multiplexing master must not keep the forward alive after the
        // owned child exits, and saved values may only form the final destination argument.
        let args = webhook_ssh_args(&fixture(), "http://127.0.0.1:34001").unwrap();
        let options: Vec<_> = args
            .windows(2)
            .filter(|pair| pair[0] == "-o")
            .map(|pair| pair[1].as_str())
            .collect();
        assert!(options.contains(&"ControlMaster=no") && options.contains(&"ControlPath=none"));
        assert_eq!(args.last().unwrap(), "dialer@forwarder.example.test");
        assert!(args[..args.len() - 1]
            .iter()
            .all(|arg| !arg.contains("forwarder") && !arg.contains("dialer")));
    }

    #[test]
    fn webhook_tunnel_missing_ssh_is_an_actionable_state() {
        let error =
            spawn_webhook_ssh("/missing/opendialer-ssh-fixture", &["-NT".into()]).unwrap_err();
        assert_eq!(error, "ssh_missing");
    }

    #[cfg(unix)]
    #[test]
    fn webhook_supervisor_reaps_stale_child_and_can_be_stopped_during_backoff() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        let starts = Arc::new(AtomicUsize::new(0));
        let observed = starts.clone();
        let mut supervisor = TunnelSupervisor::start(move || {
            observed.fetch_add(1, Ordering::SeqCst);
            std::process::Command::new("false")
                .spawn()
                .map(|child| OwnedWebhookTunnel {
                    child: Some(child),
                    group: false,
                })
                .map_err(|_| "ssh_missing".into())
        });
        let deadline = std::time::Instant::now() + Duration::from_secs(4);
        while starts.load(Ordering::SeqCst) < 2 && std::time::Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(starts.load(Ordering::SeqCst) >= 2);
        let before = std::time::Instant::now();
        supervisor.stop().unwrap();
        assert!(before.elapsed() < Duration::from_millis(500));
        let count = starts.load(Ordering::SeqCst);
        std::thread::sleep(Duration::from_millis(50));
        assert_eq!(starts.load(Ordering::SeqCst), count);
    }

    #[cfg(unix)]
    #[test]
    fn webhook_tunnel_stop_also_terminates_ssh_helpers_in_its_owned_group() {
        let root = temp_root("ssh-group");
        let pid_file = root.join("helper.pid");
        let script = format!("sleep 30 & echo $! > '{}'; wait", pid_file.display());
        let mut tunnel = spawn_webhook_ssh("/bin/sh", &["-c".into(), script]).unwrap();
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        let helper = loop {
            if let Some(pid) = std::fs::read_to_string(&pid_file)
                .ok()
                .and_then(|text| text.trim().parse::<u32>().ok())
            {
                break pid;
            }
            assert!(std::time::Instant::now() < deadline, "helper never started");
            std::thread::sleep(Duration::from_millis(10));
        };
        tunnel.stop().unwrap();
        let alive = |pid: u32| {
            std::process::Command::new("kill")
                .args(["-0", &pid.to_string()])
                .stderr(std::process::Stdio::null())
                .status()
                .unwrap()
                .success()
        };
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        while alive(helper) && std::time::Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }
        let survived = alive(helper);
        if survived {
            let _ = std::process::Command::new("kill")
                .arg(helper.to_string())
                .status();
        }
        std::fs::remove_dir_all(root).unwrap();
        assert!(
            !survived,
            "an SSH helper (ProxyCommand/ProxyJump) must not outlive the owned tunnel"
        );
    }
}
