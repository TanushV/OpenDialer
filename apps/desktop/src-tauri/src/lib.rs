use serde::{Deserialize, Serialize};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{Manager, State};
use tauri_plugin_shell::{process::CommandChild, ShellExt};
use uuid::Uuid;

struct Backend {
    config: BackendConfig,
    child: CommandChild,
}
#[derive(Clone, Serialize)]
struct BackendConfig {
    url: String,
    local_token: String,
    simulation: bool,
}
#[derive(Default, Clone)]
struct LocalBackend(Arc<Mutex<Option<Backend>>>);

#[derive(Clone, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct AuthorityBinding {
    url: String,
    token: String,
    tenant_id: String,
    local: bool,
    simulation: bool,
}
#[derive(Default)]
struct AuthorityState {
    binding: Option<AuthorityBinding>,
    prompt: Option<OwnedWebhookTunnel>,
}
#[derive(Default, Clone)]
struct NativeAuthority(Arc<Mutex<AuthorityState>>);

#[derive(Deserialize, Serialize)]
struct ConfiguredProviderState {
    version: u64,
    configured: std::collections::BTreeMap<String, bool>,
}

fn provider_input_label(field: &str) -> Result<&'static str, String> {
    match field {
        "telnyx_api_key" => Ok("Telnyx API key"),
        "openrouter_api_key" => Ok("OpenRouter API key"),
        "openai_api_key" => Ok("OpenAI / LiteLLM API key"),
        "dnc_api_key" => Ok("Prepaid DNC API key"),
        _ => Err("This field has no protected provider input".into()),
    }
}

fn authority_client() -> Result<reqwest::blocking::Client, String> {
    let _ = rustls::crypto::ring::default_provider().install_default();
    reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(8))
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Authority transport unavailable".into())
}

fn authority_response(response: reqwest::blocking::Response) -> Result<Vec<u8>, String> {
    use std::io::Read;
    match response.status().as_u16() {
        200 => (),
        401 => return Err("Session expired; reconnect to this workspace".into()),
        403 => return Err("Administrator access is required for this operation".into()),
        409 => return Err("Workspace changed or has unfinished obligations; reload and resolve them before retrying".into()),
        _ => return Err("Authority refused the operation; no automatic retry was made".into()),
    }
    let mut body = Vec::new();
    response
        .take(65537)
        .read_to_end(&mut body)
        .map_err(|_| "Authority response interrupted")?;
    if body.len() > 65536 {
        return Err("Authority response exceeds the supported bound".into());
    }
    Ok(body)
}

fn save_provider_key_to(
    authority: &AuthorityBinding,
    field: &str,
    secret: &str,
    version: u64,
) -> Result<ConfiguredProviderState, String> {
    provider_input_label(field)?;
    validate_backend_url(&authority.url)?;
    if secret.is_empty() || secret.len() > 2048 || !secret.bytes().all(|v| v.is_ascii_graphic()) {
        return Err(
            "Enter a nonempty provider key of at most 2048 printable ASCII characters".into(),
        );
    }
    let mut body =
        serde_json::json!({"request_id": Uuid::new_v4().to_string(), "expected_version": version});
    body[field] = serde_json::Value::String(secret.to_string());
    let response = authority_client()?
        .put(format!(
            "{}/v1/admin/settings",
            authority.url.trim_end_matches('/')
        ))
        .bearer_auth(&authority.token)
        .json(&body)
        .send()
        .map_err(|_| "Save response unavailable; reload configured state before retrying")?;
    serde_json::from_slice(&authority_response(response)?)
        .map_err(|_| "Authority returned invalid configured state".into())
}

#[tauri::command]
async fn bind_authority(app: tauri::AppHandle, binding: AuthorityBinding) -> Result<(), String> {
    secure_io(move || {
        validate_backend_url(&binding.url)?;
        if binding.token.is_empty() || binding.token.len() > 16000 || binding.tenant_id.len() > 100
        {
            return Err("Invalid workspace identity".into());
        }
        if binding.local {
            let local = app.state::<LocalBackend>();
            let state = local.0.lock().map_err(|_| "Local authority unavailable")?;
            if state.as_ref().is_none_or(|r| {
                r.config.url != binding.url || r.config.simulation != binding.simulation
            }) {
                return Err("This is not the managed local authority".into());
            }
        }
        let native = app.state::<NativeAuthority>();
        let mut state = native
            .0
            .lock()
            .map_err(|_| "Workspace identity unavailable")?;
        if let Some(current) = &state.binding {
            if current.url != binding.url
                || current.tenant_id != binding.tenant_id
                || current.local != binding.local
            {
                return Err("Sign out of the current workspace before changing authority".into());
            }
        }
        state.binding = Some(binding);
        Ok(())
    })
    .await
}

fn protected_prompt(
    authority: &NativeAuthority,
    binding: &AuthorityBinding,
    label: &str,
) -> Result<String, String> {
    use std::io::Read;
    let target = url::Url::parse(&binding.url).map_err(|_| "Invalid authority")?;
    let title = format!(
        "OpenDialer — {label}\nSave only to {}",
        target.origin().ascii_serialization()
    );
    #[cfg(target_os = "macos")]
    let mut command = {
        let mut command = std::process::Command::new("/usr/bin/osascript");
        command.args(["-e", "on run argv\nset result to display dialog (item 1 of argv) default answer \"\" with hidden answer buttons {\"Cancel\", \"Save\"} default button \"Save\" giving up after 120\nif gave up of result then error number -128\nreturn text returned of result\nend run", &title]);
        command
    };
    #[cfg(windows)]
    let mut command = {
        let program = std::path::PathBuf::from(
            std::env::var_os("SystemRoot").ok_or("Windows system directory unavailable")?,
        )
        .join("System32/WindowsPowerShell/v1.0/powershell.exe");
        let mut command = std::process::Command::new(program);
        command.args(["-NoProfile", "-NonInteractive", "-STA", "-Command", r#"
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$form = New-Object Windows.Forms.Form
$form.Text = 'OpenDialer protected credential'; $form.Width = 520; $form.Height = 210; $form.StartPosition = 'CenterScreen'
$label = New-Object Windows.Forms.Label; $label.Text = $env:OPENDIALER_INPUT_LABEL; $label.SetBounds(15,15,480,55)
$input = New-Object Windows.Forms.TextBox; $input.UseSystemPasswordChar = $true; $input.MaxLength = 2048; $input.SetBounds(15,75,475,25)
$save = New-Object Windows.Forms.Button; $save.Text = 'Save'; $save.SetBounds(300,115,90,30); $save.DialogResult = [Windows.Forms.DialogResult]::OK
$cancel = New-Object Windows.Forms.Button; $cancel.Text = 'Cancel'; $cancel.SetBounds(400,115,90,30); $cancel.DialogResult = [Windows.Forms.DialogResult]::Cancel
$form.Controls.AddRange(@($label,$input,$save,$cancel)); $form.AcceptButton = $save; $form.CancelButton = $cancel
$timer = New-Object Windows.Forms.Timer; $timer.Interval = 120000; $timer.Add_Tick({ $form.Close() }); $timer.Start()
if ($form.ShowDialog() -eq [Windows.Forms.DialogResult]::OK) { [Console]::Write($input.Text) }
$input.Clear(); $timer.Dispose(); $form.Dispose()
"#]).env("OPENDIALER_INPUT_LABEL", title);
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
        command
    };
    #[cfg(not(any(target_os = "macos", windows)))]
    let mut command = {
        let _ = title;
        return Err("Protected input is supported by the Mac and Windows desktop".into());
        #[allow(unreachable_code)]
        std::process::Command::new("false")
    };
    command
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null());
    {
        let mut state = authority.0.lock().map_err(|_| "Workspace unavailable")?;
        if state.binding.as_ref() != Some(binding) || state.prompt.is_some() {
            return Err("Workspace changed or another protected entry is active".into());
        }
        state.prompt = Some(OwnedWebhookTunnel::new(
            command
                .spawn()
                .map_err(|_| "OS protected input unavailable")?,
        ));
    }
    let end = std::time::Instant::now() + Duration::from_secs(125);
    loop {
        {
            let mut state = authority.0.lock().map_err(|_| "Workspace unavailable")?;
            let same = state.binding.as_ref() == Some(binding);
            let Some(prompt) = state.prompt.as_mut() else {
                return Err("Credential entry canceled".into());
            };
            if !same || std::time::Instant::now() >= end {
                prompt.stop()?;
                state.prompt = None;
                return Err("Credential entry expired or workspace changed".into());
            }
            match prompt.child.as_mut().unwrap().try_wait() {
                Ok(Some(exit)) => {
                    let mut prompt = state.prompt.take().unwrap();
                    let mut secret = String::new();
                    if exit.success() {
                        prompt
                            .child
                            .as_mut()
                            .unwrap()
                            .stdout
                            .take()
                            .ok_or("Protected input unavailable")?
                            .take(4097)
                            .read_to_string(&mut secret)
                            .map_err(|_| "Protected input interrupted")?;
                    }
                    prompt.stop()?;
                    let secret = secret.trim_end_matches(['\r', '\n']).to_string();
                    if secret.is_empty() {
                        return Err("Credential entry canceled".into());
                    }
                    return Ok(secret);
                }
                Ok(None) => (),
                Err(_) => {
                    prompt.stop()?;
                    state.prompt = None;
                    return Err("Protected input interrupted".into());
                }
            }
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

#[tauri::command]
async fn protected_provider_input(
    app: tauri::AppHandle,
    field: String,
    expected_version: u64,
) -> Result<ConfiguredProviderState, String> {
    secure_io(move || {
        let label = provider_input_label(&field)?;
        let native = app.state::<NativeAuthority>();
        let binding = native
            .0
            .lock()
            .map_err(|_| "Workspace unavailable")?
            .binding
            .clone()
            .ok_or("Choose a workspace first")?;
        // Verify administrator access before presenting the protected entry dialog.
        let response = authority_client()?
            .get(format!(
                "{}/v1/admin/settings",
                binding.url.trim_end_matches('/')
            ))
            .bearer_auth(&binding.token)
            .send()
            .map_err(|_| "Authority is unavailable")?;
        let before: ConfiguredProviderState =
            serde_json::from_slice(&authority_response(response)?)
                .map_err(|_| "Invalid authority state")?;
        if before.version != expected_version {
            return Err("Provider configuration changed; reload before entering a key".into());
        }
        let secret = protected_prompt(&native, &binding, label)?;
        if native
            .0
            .lock()
            .map_err(|_| "Workspace unavailable")?
            .binding
            .as_ref()
            != Some(&binding)
        {
            return Err("Workspace changed; credential was not sent".into());
        }
        save_provider_key_to(&binding, &field, &secret, expected_version)
    })
    .await
}

#[tauri::command]
async fn disconnect_authority(app: tauri::AppHandle) -> Result<(), String> {
    secure_io(move || {
        let native = app.state::<NativeAuthority>();
        let mut state = native.0.lock().map_err(|_| "Workspace unavailable")?;
        let binding = state.binding.as_ref().ok_or("No active native workspace")?;
        let (root, path) = cache_path(
            &app,
            &format!("optouts-{}", binding.tenant_id),
            binding.simulation,
        )?;
        if path.exists() {
            let bytes = std::fs::read(path).map_err(|_| "Cannot inspect pending opt-outs")?;
            let clear = decrypt_blob(
                &master_key(binding.simulation, &root)?,
                &format!("optouts-{}", binding.tenant_id),
                &bytes,
            )?;
            let pending: Vec<serde_json::Value> =
                serde_json::from_slice(&clear).map_err(|_| "Opt-out journal requires recovery")?;
            if !pending.is_empty() {
                return Err("Synchronize pending opt-outs before changing workspace".into());
            }
        }
        let response = authority_client()?
            .post(format!(
                "{}/v1/auth/logout",
                binding.url.trim_end_matches('/')
            ))
            .bearer_auth(&binding.token)
            .send()
            .map_err(|_| {
                "Authority unavailable; remain in this workspace until obligations can be checked"
            })?;
        authority_response(response)?;
        if let Some(mut prompt) = state.prompt.take() {
            prompt.stop()?;
        }
        app.state::<WebhookTunnel>().stop()?;
        if let Some(backend) = app
            .state::<LocalBackend>()
            .0
            .lock()
            .map_err(|_| "Local authority unavailable")?
            .take()
        {
            backend
                .child
                .kill()
                .map_err(|_| "Cannot stop previous local authority")?;
        }
        state.binding = None;
        Ok(())
    })
    .await
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
}
impl OwnedWebhookTunnel {
    fn new(child: std::process::Child) -> Self {
        Self { child: Some(child) }
    }
    #[cfg(test)]
    fn is_stopped(&self) -> bool {
        self.child.is_none()
    }
    fn stop(&mut self) -> Result<(), String> {
        if let Some(child) = self.child.as_mut() {
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
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    command.spawn().map(OwnedWebhookTunnel::new).map_err(|e| {
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
    use std::io::Write;
    validate_tunnel(config)?;
    let parent = path.parent().ok_or("Invalid forwarding location")?;
    std::fs::create_dir_all(parent).map_err(|_| "Cannot create forwarding configuration")?;
    let temp = path.with_extension(format!("{}.tmp", Uuid::new_v4()));
    let result = (|| {
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options
            .open(&temp)
            .map_err(|_| "Cannot create forwarding configuration")?;
        file.write_all(&serde_json::to_vec(config).map_err(|_| "Invalid configuration")?)
            .map_err(|_| "Cannot write forwarding configuration")?;
        file.sync_all()
            .map_err(|_| "Cannot flush forwarding configuration")?;
        std::fs::rename(&temp, path).map_err(|_| "Cannot publish forwarding configuration")?;
        #[cfg(unix)]
        std::fs::File::open(parent)
            .and_then(|f| f.sync_all())
            .map_err(|_| "Cannot flush forwarding directory")?;
        Ok(())
    })();
    let _ = std::fs::remove_file(temp);
    result
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
    secure_io(move || {
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
    secure_io(move || {
        validate_tunnel(&config)?;
        let state = app.state::<LocalBackend>();
        let runtime = state.0.lock().map_err(|_| "Local authority unavailable")?.as_ref().map(|b| b.config.clone());
        if config.enabled && runtime.as_ref().is_none_or(|r| r.simulation) {
            return Err("Enable forwarding from the actual local workspace, not simulation or connected mode".into());
        }
        save_tunnel_config(&app.path().app_data_dir().map_err(|_| "App data unavailable")?.join("webhook-forward.json"), &config)?;
        app.state::<WebhookTunnel>().stop()?;
        if let Some(runtime) = runtime { sync_tunnel(&app, &runtime)?; }
        Ok(())
    }).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

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
    }

    #[cfg(unix)]
    #[test]
    fn webhook_tunnel_cleanup_waits_for_only_the_owned_child() {
        let owned = std::process::Command::new("sleep")
            .arg("30")
            .spawn()
            .unwrap();
        let mut unrelated = std::process::Command::new("sleep")
            .arg("30")
            .spawn()
            .unwrap();
        let mut tunnel = OwnedWebhookTunnel::new(owned);
        let result = tunnel.stop();
        let unrelated_alive = unrelated.try_wait().unwrap().is_none();
        // Always reap both test-owned children even when an assertion fails.
        let _ = unrelated.kill();
        let _ = unrelated.wait();
        assert!(result.is_ok());
        assert!(tunnel.is_stopped());
        assert!(unrelated_alive);
        assert!(
            tunnel.stop().is_ok(),
            "cleanup is idempotent for quit/mode/update"
        );
    }

    #[test]
    fn webhook_tunnel_missing_ssh_is_an_actionable_state() {
        let error =
            spawn_webhook_ssh("/missing/opendialer-ssh-fixture", &["-NT".into()]).unwrap_err();
        assert_eq!(error, "ssh_missing");
    }

    #[test]
    fn webhook_tunnel_saved_config_is_validated_and_contains_no_runtime_identity() {
        let root = std::env::temp_dir().join(format!("opendialer-tunnel-{}", Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let path = root.join("webhook-forward.json");
        save_tunnel_config(&path, &fixture()).unwrap();
        let saved = load_tunnel_config(&path).unwrap();
        assert_eq!(saved.host, fixture().host);
        let mut bad = fixture();
        bad.remote_port = 0;
        assert!(save_tunnel_config(&path, &bad).is_err());
        assert_eq!(load_tunnel_config(&path).unwrap().remote_port, 18765);
        assert!(!std::fs::read_to_string(&path).unwrap().contains("token"));
        std::fs::remove_dir_all(root).unwrap();
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
                .map(OwnedWebhookTunnel::new)
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

    #[test]
    fn authority_binding_guards_local_runtime_and_forwarding() {
        let runtime = BackendConfig {
            url: "http://127.0.0.1:33001".into(),
            local_token: "local".into(),
            simulation: false,
        };
        let mut binding = AuthorityBinding {
            url: runtime.url.clone(),
            token: "session".into(),
            tenant_id: "tenant".into(),
            local: true,
            simulation: false,
        };
        assert!(guard_local_mode(Some(&binding), false).is_ok());
        assert!(guard_local_mode(Some(&binding), true).is_err());
        assert!(guard_forwarding(Some(&binding), Some(&runtime)).is_ok());
        binding.local = false;
        assert!(guard_local_mode(Some(&binding), false).is_err());
        assert!(guard_forwarding(Some(&binding), Some(&runtime)).is_err());
        binding.local = true;
        binding.url = "http://127.0.0.1:33002".into();
        assert!(guard_forwarding(Some(&binding), Some(&runtime)).is_err());
        assert!(guard_forwarding(None, Some(&runtime)).is_err());
        assert!(guard_local_mode(None, true).is_ok());
    }

    #[test]
    fn protected_credential_redirect_is_not_followed() {
        use std::io::{Read, Write};
        let sink = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let target = sink.local_addr().unwrap();
        sink.set_nonblocking(true).unwrap();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            socket
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            let mut bytes = [0; 4096];
            let _ = socket.read(&mut bytes).unwrap();
            write!(socket,"HTTP/1.1 307 Temporary Redirect\r\nLocation: http://{target}/steal\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").unwrap();
        });
        let binding = AuthorityBinding {
            url: format!("http://{address}"),
            token: "fixture".into(),
            tenant_id: "t".into(),
            local: false,
            simulation: false,
        };
        assert!(save_provider_key_to(&binding, "openrouter_api_key", "synthetic-only", 0).is_err());
        server.join().unwrap();
        assert!(sink.accept().is_err());
    }

    #[test]
    fn protected_provider_input_has_an_exact_secret_allowlist() {
        for field in [
            "telnyx_api_key",
            "openrouter_api_key",
            "openai_api_key",
            "dnc_api_key",
        ] {
            assert!(provider_input_label(field).is_ok());
        }
        for field in [
            "url",
            "chatgpt_access_token",
            "groq_api_key",
            "x\nWrite-Output bad",
        ] {
            assert!(provider_input_label(field).is_err());
        }
    }

    #[test]
    fn protected_provider_save_sends_to_bound_authority_and_returns_only_redacted_state() {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            socket
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            let mut request = Vec::new();
            let mut buffer = [0; 4096];
            loop {
                let n = socket.read(&mut buffer).unwrap();
                request.extend_from_slice(&buffer[..n]);
                if request.windows(21).any(|x| x == b"synthetic-secret-only") {
                    break;
                }
            }
            let body = r#"{"version":2,"configured":{"openrouter_api_key":true},"unexpected":"secret-must-not-reach-renderer"}"#;
            write!(
                socket,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .unwrap();
            String::from_utf8(request).unwrap()
        });
        let authority = AuthorityBinding {
            url: format!("http://{address}"),
            token: "synthetic-session".into(),
            local: false,
            simulation: false,
            tenant_id: "fixture".into(),
        };
        let result =
            save_provider_key_to(&authority, "openrouter_api_key", "synthetic-secret-only", 1)
                .unwrap();
        assert_eq!(result.version, 2);
        let shown = serde_json::to_string(&result).unwrap();
        assert!(!shown.contains("secret-must") && !shown.contains("synthetic-secret"));
        let request = server.join().unwrap();
        assert!(request.starts_with("PUT /v1/admin/settings HTTP/1.1"));
        assert!(request.contains("Bearer synthetic-session"));
        assert!(request.contains("expected_version") && request.contains("request_id"));
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

pub fn validate_backend_url(value: &str) -> Result<(), String> {
    let parsed = url::Url::parse(value).map_err(|_| "Invalid server URL".to_string())?;
    let local = matches!(parsed.host_str(), Some("127.0.0.1" | "localhost" | "[::1]"));
    if parsed.host_str().is_none()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
        || !(parsed.scheme() == "https" || (parsed.scheme() == "http" && local))
    {
        return Err("Use HTTPS for a connected server; HTTP is allowed only on loopback".into());
    }
    Ok(())
}

fn secret_entry(account: &str) -> Result<keyring::Entry, String> {
    keyring::Entry::new("com.opendialer.desktop", account)
        .map_err(|_| "OS credential store is unavailable".into())
}

fn master_key(simulation: bool, directory: &std::path::Path) -> Result<String, String> {
    if simulation {
        use std::io::Write;
        let path = directory.join("simulation-master");
        if path.exists() {
            return std::fs::read_to_string(path)
                .map_err(|_| "Cannot read the simulation data key".into());
        }
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options
            .open(path)
            .map_err(|_| "Cannot create a private simulation key")?;
        let key = format!("{}{}", Uuid::new_v4(), Uuid::new_v4());
        file.write_all(key.as_bytes())
            .map_err(|_| "Cannot persist simulation key")?;
        file.sync_all().map_err(|_| "Cannot flush simulation key")?;
        return Ok(key);
    }
    let entry = secret_entry("authority-master-v1")?;
    match entry.get_password() {
        Ok(value) => Ok(value),
        Err(keyring::Error::NoEntry) => {
            let value = format!("{}{}", Uuid::new_v4(), Uuid::new_v4());
            entry.set_password(&value).map_err(|_| {
                "Cannot protect the local authority key in the OS credential store".to_string()
            })?;
            Ok(value)
        }
        Err(_) => Err("Unlock your OS credential store to start OpenDialer".into()),
    }
}

pub fn managed_sidecar(
    command: tauri_plugin_shell::process::Command,
) -> tauri_plugin_shell::process::Command {
    command.set_process_group(true)
}

pub async fn secure_io<T: Send + 'static, F: FnOnce() -> Result<T, String> + Send + 'static>(
    operation: F,
) -> Result<T, String> {
    tauri::async_runtime::spawn_blocking(operation)
        .await
        .map_err(|_| "Protected storage operation was interrupted".to_string())?
}

#[tauri::command]
async fn backend_config(
    app: tauri::AppHandle,
    state: State<'_, LocalBackend>,
    simulation: bool,
) -> Result<BackendConfig, String> {
    let backend = state.inner().clone();
    secure_io(move || {
        let runtime = start_backend(app.clone(), backend, simulation)?;
        sync_tunnel(&app, &runtime)?;
        Ok(runtime)
    })
    .await
}

fn start_backend(
    app: tauri::AppHandle,
    state: LocalBackend,
    simulation: bool,
) -> Result<BackendConfig, String> {
    let mut locked = state
        .0
        .lock()
        .map_err(|_| "Local service state unavailable")?;
    if let Some(existing) = locked.as_ref() {
        if existing.config.simulation == simulation {
            return Ok(existing.config.clone());
        }
    }
    if let Some(old) = locked.take() {
        app.state::<WebhookTunnel>().stop()?;
        old.child
            .kill()
            .map_err(|_| "Cannot stop the previous local service")?;
    }
    let directory = app
        .path()
        .app_data_dir()
        .map_err(|_| "App data directory unavailable")?
        .join(if simulation { "simulation" } else { "local" });
    std::fs::create_dir_all(&directory).map_err(|_| "Cannot create private application data")?;
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
    ];
    if simulation {
        args.push("--simulation".into());
    }
    let command = managed_sidecar(
        app.shell()
            .sidecar("opendialer-backend")
            .map_err(|_| "Bundled local service is missing")?,
    )
    .args(args)
    .env("OPENDIALER_LOCAL_TOKEN", &nonce)
    .env("OPENDIALER_SECRET_KEY", master_key(simulation, &directory)?)
    .env("OMP_NUM_THREADS", "1")
    .env("OPENBLAS_NUM_THREADS", "1");
    let (mut receiver, child) = command
        .spawn()
        .map_err(|_| "Cannot start the bundled local service")?;
    tauri::async_runtime::spawn(async move {
        // Drain without forwarding provider or user content into ordinary desktop logs.
        while receiver.recv().await.is_some() {}
    });
    let config = BackendConfig {
        url: format!("http://127.0.0.1:{port}"),
        local_token: nonce,
        simulation,
    };
    *locked = Some(Backend {
        config: config.clone(),
        child,
    });
    Ok(config)
}

#[tauri::command]
async fn save_session(value: String) -> Result<(), String> {
    secure_io(move || save_session_sync(value)).await
}
fn save_session_sync(value: String) -> Result<(), String> {
    if value.len() > 16000 {
        return Err("Session exceeds secure storage limit".into());
    }
    secret_entry("active-session-v1")?
        .set_password(&value)
        .map_err(|_| "Cannot save session in OS credential store".into())
}
#[tauri::command]
async fn load_session() -> Result<Option<String>, String> {
    secure_io(load_session_sync).await
}
fn load_session_sync() -> Result<Option<String>, String> {
    match secret_entry("active-session-v1")?.get_password() {
        Ok(value) => Ok(Some(value)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(_) => Err("Cannot read protected session".into()),
    }
}
#[tauri::command]
async fn clear_session() -> Result<(), String> {
    secure_io(clear_session_sync).await
}
fn clear_session_sync() -> Result<(), String> {
    match secret_entry("active-session-v1")?.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(_) => Err("Cannot clear protected session".into()),
    }
}
#[tauri::command]
fn check_server_url(value: String) -> Result<(), String> {
    validate_backend_url(&value)
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

pub fn encrypt_blob(secret: &str, namespace: &str, value: &[u8]) -> Result<Vec<u8>, String> {
    use aes_gcm::{
        aead::{Aead, AeadCore, KeyInit, OsRng, Payload},
        Aes256Gcm,
    };
    use sha2::{Digest, Sha256};
    let key = Sha256::digest(format!("{secret}:desktop-cache:v1").as_bytes());
    let cipher = Aes256Gcm::new_from_slice(&key).map_err(|_| "Cache key unavailable")?;
    let nonce = Aes256Gcm::generate_nonce(&mut OsRng);
    let encrypted = cipher
        .encrypt(
            &nonce,
            Payload {
                msg: value,
                aad: namespace.as_bytes(),
            },
        )
        .map_err(|_| "Cannot encrypt cache")?;
    Ok([b"ODC1".as_slice(), nonce.as_slice(), encrypted.as_slice()].concat())
}

pub fn decrypt_blob(secret: &str, namespace: &str, value: &[u8]) -> Result<Vec<u8>, String> {
    use aes_gcm::{
        aead::{Aead, KeyInit, Payload},
        Aes256Gcm, Nonce,
    };
    use sha2::{Digest, Sha256};
    if value.len() < 32 || &value[..4] != b"ODC1" {
        return Err("Invalid encrypted cache".into());
    }
    let key = Sha256::digest(format!("{secret}:desktop-cache:v1").as_bytes());
    let cipher = Aes256Gcm::new_from_slice(&key).map_err(|_| "Cache key unavailable")?;
    cipher
        .decrypt(
            Nonce::from_slice(&value[4..16]),
            Payload {
                msg: &value[16..],
                aad: namespace.as_bytes(),
            },
        )
        .map_err(|_| "Cache authentication failed".into())
}

fn cache_path(
    app: &tauri::AppHandle,
    namespace: &str,
    simulation: bool,
) -> Result<(std::path::PathBuf, std::path::PathBuf), String> {
    if namespace.is_empty()
        || namespace.len() > 120
        || !namespace
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
    {
        return Err("Invalid cache namespace".into());
    }
    let root = app
        .path()
        .app_data_dir()
        .map_err(|_| "App data unavailable")?
        .join(if simulation { "simulation" } else { "local" });
    let directory = root.join("encrypted-cache");
    std::fs::create_dir_all(&directory).map_err(|_| "Cache directory unavailable")?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700))
            .map_err(|_| "Cannot protect data directory")?;
    }
    Ok((root, directory.join(format!("{namespace}.enc"))))
}

#[tauri::command]
async fn cache_read(
    app: tauri::AppHandle,
    namespace: String,
    simulation: bool,
) -> Result<Option<String>, String> {
    secure_io(move || {
        let (root, path) = cache_path(&app, &namespace, simulation)?;
        if !path.exists() {
            return Ok(None);
        }
        let secret = master_key(simulation, &root)?;
        let bytes = std::fs::read(path).map_err(|_| "Cannot read cache")?;
        String::from_utf8(decrypt_blob(&secret, &namespace, &bytes)?)
            .map(Some)
            .map_err(|_| "Invalid cache content".into())
    })
    .await
}

#[tauri::command]
async fn cache_write(
    app: tauri::AppHandle,
    namespace: String,
    value: String,
    simulation: bool,
) -> Result<(), String> {
    secure_io(move || {
        use std::io::Write;
        if value.len() > 2_000_000 {
            return Err("Cache exceeds the bounded storage limit".into());
        }
        let (root, path) = cache_path(&app, &namespace, simulation)?;
        let secret = master_key(simulation, &root)?;
        let temp = path.with_extension(format!("{}.tmp", Uuid::new_v4()));
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(&temp).map_err(|_| "Cannot persist cache")?;
        file.write_all(&encrypt_blob(&secret, &namespace, value.as_bytes())?)
            .map_err(|_| "Cannot write cache")?;
        file.sync_all().map_err(|_| "Cannot flush cache")?;
        std::fs::rename(temp, path).map_err(|_| "Cannot publish cache atomically".into())
    })
    .await
}

/// Legacy machine snapshots may contain call text. The authoritative records and opt-out
/// journal are separate; never remove them during the short-retention cache cutover.
pub fn purge_legacy_caches(directory: &std::path::Path) -> Result<usize, String> {
    if !directory.exists() {
        return Ok(0);
    }
    let mut removed = 0;
    for entry in std::fs::read_dir(directory).map_err(|_| "Cannot inspect legacy caches")? {
        let entry = entry.map_err(|_| "Cannot inspect legacy cache entry")?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if ["analysis-", "packet-", "workspace-"]
            .iter()
            .any(|prefix| name.starts_with(prefix))
            && !entry
                .file_type()
                .map_err(|_| "Cannot inspect cache type")?
                .is_dir()
        {
            std::fs::remove_file(entry.path()).map_err(|_| "Cannot remove legacy raw cache")?;
            removed += 1;
        }
    }
    Ok(removed)
}

pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_opener::init())
        .manage(LocalBackend::default())
        .manage(WebhookTunnel::default())
        .manage(NativeAuthority::default())
        .setup(|app| {
            let root = app.path().app_data_dir()?;
            for mode in ["local", "simulation"] {
                purge_legacy_caches(&root.join(mode).join("encrypted-cache"))
                    .map_err(std::io::Error::other)?;
            }
            let backend = app.state::<LocalBackend>().inner().clone();
            let tunnel = app.state::<WebhookTunnel>().inner().clone();
            let authority = app.state::<NativeAuthority>().inner().clone();
            register_exit_cleanup(app.handle(), move || {
                if let Ok(mut state) = authority.0.lock() {
                    if let Some(mut prompt) = state.prompt.take() {
                        let _ = prompt.stop();
                    }
                    state.binding = None;
                }
                let _ = tunnel.stop();
                if let Ok(mut state) = backend.0.lock() {
                    if let Some(backend) = state.take() {
                        let _ = backend.child.kill();
                    }
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            backend_config,
            save_session,
            load_session,
            clear_session,
            check_server_url,
            open_source,
            cache_read,
            cache_write,
            webhook_tunnel_status,
            webhook_tunnel_set,
            bind_authority,
            protected_provider_input,
            disconnect_authority
        ])
        .build(tauri::generate_context!())
        .expect("OpenDialer could not start");
    app.run(|handle, event| {
        if let tauri::RunEvent::Exit = event {
            let _ = handle.state::<WebhookTunnel>().stop();
            if let Ok(mut state) = handle.state::<LocalBackend>().0.lock() {
                if let Some(backend) = state.take() {
                    let _ = backend.child.kill();
                }
            }
        }
    });
}
