use serde::Serialize;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
#[cfg(windows)]
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
    blocking(move || start_backend(&app, &backend, simulation)).await
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
            // Reap any remaining group/job members; the leader has already exited.
            let _ = backend.child.kill();
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

#[tauri::command]
async fn save_registry_csv(app: tauri::AppHandle, csv: String) -> Result<String, String> {
    use tauri_plugin_opener::OpenerExt;

    let exports = app
        .path()
        .app_data_dir()
        .map_err(|err| err.to_string())?
        .join("Exports");
    tauri::async_runtime::spawn_blocking(move || {
        std::fs::create_dir_all(&exports).map_err(|err| err.to_string())?;
        let path = exports.join(format!("opendialer-caller-registry-{}.csv", Uuid::new_v4()));
        std::fs::write(&path, csv).map_err(|err| err.to_string())?;
        // A failed Finder/Explorer reveal does not undo the local export.
        let _ = app.opener().reveal_item_in_dir(&path);
        Ok(path.to_string_lossy().into_owned())
    })
    .await
    .map_err(|err| err.to_string())?
}

/// Quit, mode change and updater cleanup: terminate the authority's process group (Unix) or
/// job object (Windows) and wait for it.
fn stop_owned_runtimes(backend: &LocalBackend) {
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
    for name in [
        "opendialer-backend.exe",
        "opendialer-trainer.exe",
        "opendialer-codex.exe",
        "codex-code-mode-host.exe",
    ] {
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
        .setup(|app| {
            // The updater clears the resource table before it launches an installer and exits,
            // which runs this guard.
            let backend = app.state::<LocalBackend>().inner().clone();
            register_exit_cleanup(app.handle(), move || stop_owned_runtimes(&backend));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            backend_config,
            open_source,
            save_registry_csv
        ])
        .build(tauri::generate_context!())
        .expect("OpenDialer could not start");
    app.run(|handle, event| {
        if let tauri::RunEvent::Exit = event {
            stop_owned_runtimes(&handle.state::<LocalBackend>());
        }
    });
}
