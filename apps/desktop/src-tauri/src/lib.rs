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

// Lock order: NativeAuthority -> LocalBackend -> WebhookTunnel. None of them is held across
// OS credential-store access or authority HTTP, so a keychain prompt cannot freeze commands.

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
    logout_confirmed: bool,
}
#[derive(Default, Clone)]
struct NativeAuthority(Arc<Mutex<AuthorityState>>);

#[derive(Deserialize, Serialize)]
struct ConfiguredProviderState {
    version: u64,
    configured: std::collections::BTreeMap<String, bool>,
}

fn post_logout(binding: &AuthorityBinding) -> Result<(), String> {
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
    Ok(())
}

fn guard_local_mode(binding: Option<&AuthorityBinding>, simulation: bool) -> Result<(), String> {
    if binding.is_some_and(|b| !b.local || b.simulation != simulation) {
        return Err("Sign out after resolving obligations before changing local mode".into());
    }
    Ok(())
}

fn guard_forwarding(
    binding: Option<&AuthorityBinding>,
    runtime: Option<&BackendConfig>,
) -> Result<(), String> {
    match (binding, runtime) {
        (Some(b), Some(r)) if b.local && !b.simulation && !r.simulation && b.url == r.url => Ok(()),
        _ => Err("Select the actual managed local workspace before changing forwarding".into()),
    }
}

fn verify_bound_admin(binding: &AuthorityBinding) -> Result<ConfiguredProviderState, String> {
    let response = authority_client()?
        .get(format!(
            "{}/v1/admin/settings",
            binding.url.trim_end_matches('/')
        ))
        .bearer_auth(&binding.token)
        .send()
        .map_err(|_| "Authority is unavailable")?;
    serde_json::from_slice(&authority_response(response)?)
        .map_err(|_| "Invalid authority state".into())
}

impl NativeAuthority {
    fn clear(&self) {
        if let Ok(mut state) = self.0.lock() {
            if let Some(mut prompt) = state.prompt.take() {
                let _ = prompt.stop();
            }
            state.binding = None;
            state.logout_confirmed = false;
        }
    }
    fn binding(&self) -> Result<Option<AuthorityBinding>, String> {
        Ok(self
            .0
            .lock()
            .map_err(|_| "Workspace unavailable")?
            .binding
            .clone())
    }
    /// Revoke the bound session at most once. The HTTP call runs without the workspace lock;
    /// the confirmation is recorded only if the same binding is still active.
    fn logout_once(&self, binding: &AuthorityBinding) -> Result<(), String> {
        {
            let state = self.0.lock().map_err(|_| "Workspace unavailable")?;
            if state.binding.as_ref() != Some(binding) {
                return Err("Workspace changed; reload before signing out".into());
            }
            if state.logout_confirmed {
                return Ok(());
            }
        }
        post_logout(binding)?;
        let mut state = self.0.lock().map_err(|_| "Workspace unavailable")?;
        if state.binding.as_ref() != Some(binding) {
            return Err("Workspace changed during sign-out; reload before retrying".into());
        }
        state.logout_confirmed = true;
        Ok(())
    }
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
        let native = app.state::<NativeAuthority>();
        let mut state = native
            .0
            .lock()
            .map_err(|_| "Workspace identity unavailable")?;
        if binding.local {
            let runtime = app.state::<LocalBackend>().current()?;
            if runtime
                .as_ref()
                .is_none_or(|r| r.url != binding.url || r.simulation != binding.simulation)
            {
                return Err("This is not the managed local authority".into());
            }
        }
        if let Some(current) = &state.binding {
            if current.url != binding.url
                || current.tenant_id != binding.tenant_id
                || current.local != binding.local
            {
                return Err("Sign out of the current workspace before changing authority".into());
            }
            if state.logout_confirmed && current != &binding {
                return Err("Finish signing out before starting another session".into());
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
        let before = verify_bound_admin(&binding)?;
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
        let binding = native.binding()?.ok_or("No active native workspace")?;
        let namespace = format!("optouts-{}", binding.tenant_id);
        let (root, path) = cache_path(&app, &namespace, binding.simulation)?;
        if path.exists() {
            let bytes = std::fs::read(path).map_err(|_| "Cannot inspect pending opt-outs")?;
            // Credential-store access happens without the workspace lock held.
            let clear = decrypt_blob(&master_key(binding.simulation, &root)?, &namespace, &bytes)?;
            let pending: Vec<serde_json::Value> =
                serde_json::from_slice(&clear).map_err(|_| "Opt-out journal requires recovery")?;
            if !pending.is_empty() {
                return Err("Synchronize pending opt-outs before changing workspace".into());
            }
        }
        native.logout_once(&binding)?;
        let mut state = native.0.lock().map_err(|_| "Workspace unavailable")?;
        if state.binding.as_ref() != Some(&binding) {
            return Err("Workspace changed during sign-out; reload before retrying".into());
        }
        if let Some(mut prompt) = state.prompt.take() {
            prompt.stop()?;
        }
        app.state::<WebhookTunnel>().stop()?;
        if let Some(backend) = app.state::<LocalBackend>().take()? {
            backend
                .child
                .kill()
                .map_err(|_| "Cannot stop previous local authority")?;
        }
        state.binding = None;
        state.logout_confirmed = false;
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
    fn new(child: std::process::Child) -> Self {
        Self {
            child: Some(child),
            group: false,
        }
    }
    fn new_group(child: std::process::Child) -> Self {
        Self {
            child: Some(child),
            group: cfg!(unix),
        }
    }
    #[cfg(test)]
    fn is_stopped(&self) -> bool {
        self.child.is_none()
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
        true,
    )
    .map_err(|_| "Cannot publish forwarding configuration".into())
}

/// Atomic private publish: create a 0600 temporary file, fsync it, rename it into place (or
/// hard-link it without replacing an existing file), then fsync the directory entry.
fn write_private_file(path: &std::path::Path, bytes: &[u8], replace: bool) -> Result<(), String> {
    use std::io::Write;
    let parent = path.parent().ok_or("Invalid protected file location")?;
    let temp = path.with_extension(format!("{}.tmp", Uuid::new_v4()));
    let result = (|| -> Result<(), String> {
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options
            .open(&temp)
            .map_err(|_| "Cannot create a private file")?;
        file.write_all(bytes)
            .map_err(|_| "Cannot write a private file")?;
        file.sync_all().map_err(|_| "Cannot flush a private file")?;
        drop(file);
        if replace {
            std::fs::rename(&temp, path).map_err(|_| "Cannot publish a private file atomically")?;
        } else {
            match std::fs::hard_link(&temp, path) {
                Ok(()) => (),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                    return Err("A protected file already exists and was not replaced".into())
                }
                // File systems without hard links; creation is already serialized.
                Err(_) if !path.exists() => std::fs::rename(&temp, path)
                    .map_err(|_| "Cannot publish a private file atomically")?,
                Err(_) => return Err("Cannot publish a private file atomically".into()),
            }
        }
        sync_directory(parent)
    })();
    let _ = std::fs::remove_file(&temp);
    result
}

fn sync_directory(directory: &std::path::Path) -> Result<(), String> {
    // Windows has no portable directory fsync; NTFS journals the rename metadata itself.
    #[cfg(unix)]
    std::fs::File::open(directory)
        .and_then(|d| d.sync_all())
        .map_err(|_| "Cannot flush the protected directory")?;
    #[cfg(not(unix))]
    let _ = directory;
    Ok(())
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
        let authority = app.state::<NativeAuthority>();
        let local = app.state::<LocalBackend>();
        let binding = authority.binding()?;
        let runtime = local.current()?;
        guard_forwarding(binding.as_ref(), runtime.as_ref())?;
        let binding = binding.ok_or("Choose a workspace first")?;
        // The administrator check is HTTP; it runs without the workspace lock held.
        verify_bound_admin(&binding)?;
        let state = authority.0.lock().map_err(|_| "Workspace unavailable")?;
        if state.binding.as_ref() != Some(&binding)
            || local.current()?.map(|r| r.url) != runtime.as_ref().map(|r| r.url.clone())
        {
            return Err("Workspace or local authority changed; forwarding was not changed".into());
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lost_logout_response_retries_the_same_bound_session_before_cleanup() {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            for attempt in 0..2 {
                let (mut socket, _) = listener.accept().unwrap();
                socket
                    .set_read_timeout(Some(Duration::from_secs(5)))
                    .unwrap();
                let mut bytes = [0; 4096];
                let count = socket.read(&mut bytes).unwrap();
                let request = String::from_utf8_lossy(&bytes[..count]).to_lowercase();
                assert!(request.starts_with("post /v1/auth/logout "));
                assert!(request.contains("authorization: bearer same-session"));
                if attempt == 1 {
                    socket.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 19\r\nConnection: close\r\n\r\n{\"logged_out\":true}").unwrap();
                }
            }
        });
        let binding = AuthorityBinding {
            url: format!("http://{address}"),
            token: "same-session".into(),
            tenant_id: "t".into(),
            local: false,
            simulation: true,
        };
        let native = NativeAuthority::default();
        native.0.lock().unwrap().binding = Some(binding.clone());
        assert!(native.logout_once(&binding).is_err());
        assert!(!native.0.lock().unwrap().logout_confirmed);
        assert!(native.0.lock().unwrap().binding.is_some());
        native.logout_once(&binding).unwrap();
        assert!(native.0.lock().unwrap().logout_confirmed);
        server.join().unwrap();
        native.logout_once(&binding).unwrap();
    }

    #[test]
    fn successful_logout_is_not_repeated_when_native_cleanup_needs_retry() {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            let mut bytes = [0; 4096];
            let count = socket.read(&mut bytes).unwrap();
            assert!(String::from_utf8_lossy(&bytes[..count]).starts_with("POST /v1/auth/logout "));
            socket
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
                .unwrap();
        });
        let binding = AuthorityBinding {
            url: format!("http://{address}"),
            token: "fixture".into(),
            tenant_id: "t".into(),
            local: false,
            simulation: true,
        };
        let native = NativeAuthority::default();
        native.0.lock().unwrap().binding = Some(binding.clone());
        native.logout_once(&binding).unwrap();
        server.join().unwrap();
        // The old session is revoked; the authority is deliberately no longer reachable.
        // Retrying owned-child cleanup must reuse the confirmed logout, not authenticate again.
        native.logout_once(&binding).unwrap();
    }

    #[test]
    fn slow_authority_http_does_not_hold_the_workspace_lock() {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            let mut bytes = [0; 4096];
            let _ = socket.read(&mut bytes).unwrap();
            std::thread::sleep(Duration::from_millis(800));
            socket
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
                .unwrap();
        });
        let binding = AuthorityBinding {
            url: format!("http://{address}"),
            token: "fixture".into(),
            tenant_id: "t".into(),
            local: false,
            simulation: true,
        };
        let native = NativeAuthority::default();
        native.0.lock().unwrap().binding = Some(binding.clone());
        let worker = {
            let (native, binding) = (native.clone(), binding.clone());
            std::thread::spawn(move || native.logout_once(&binding))
        };
        std::thread::sleep(Duration::from_millis(200));
        // Every other command (bind, prompt, exit cleanup) still gets the lock promptly.
        let started = std::time::Instant::now();
        drop(native.0.lock().unwrap());
        assert!(started.elapsed() < Duration::from_millis(300));
        worker.join().unwrap().unwrap();
        server.join().unwrap();
        assert!(native.0.lock().unwrap().logout_confirmed);
    }

    #[test]
    fn logout_confirmation_is_not_applied_to_a_changed_workspace() {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let binding = AuthorityBinding {
            url: format!("http://{address}"),
            token: "fixture".into(),
            tenant_id: "t".into(),
            local: false,
            simulation: true,
        };
        let native = NativeAuthority::default();
        native.0.lock().unwrap().binding = Some(binding.clone());
        let server = {
            let native = native.clone();
            std::thread::spawn(move || {
                let (mut socket, _) = listener.accept().unwrap();
                let mut bytes = [0; 4096];
                let _ = socket.read(&mut bytes).unwrap();
                native.clear(); // Workspace changes while the logout is in flight.
                socket
                    .write_all(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}",
                    )
                    .unwrap();
            })
        };
        assert!(native.logout_once(&binding).is_err());
        server.join().unwrap();
        assert!(!native.0.lock().unwrap().logout_confirmed);
    }

    struct MemoryKey {
        value: Mutex<Option<String>>,
        writes: std::sync::atomic::AtomicUsize,
    }
    impl MasterKeyStore for MemoryKey {
        fn load(&self) -> Result<Option<String>, String> {
            Ok(self.value.lock().unwrap().clone())
        }
        fn create(&self, value: &str) -> Result<(), String> {
            self.writes.fetch_add(1, Ordering::SeqCst);
            *self.value.lock().unwrap() = Some(value.into());
            Ok(())
        }
        fn recovery(&self) -> &'static str {
            "fixture recovery"
        }
    }
    fn memory_key(value: Option<&str>) -> MemoryKey {
        MemoryKey {
            value: Mutex::new(value.map(Into::into)),
            writes: Default::default(),
        }
    }

    #[test]
    fn credential_store_key_is_never_reminted_over_existing_data() {
        // Existing 0.2.1 install: the stored key is returned unchanged and never rewritten.
        let root = temp_root("keychain-existing");
        std::fs::write(root.join("opendialer.db"), b"fixture").unwrap();
        let existing = "0".repeat(72);
        let store = memory_key(Some(&existing));
        assert_eq!(resolve_master_key(&store, &root).unwrap(), existing);
        // Missing credential beside existing data: actionable error, nothing created.
        let store = memory_key(None);
        let error = resolve_master_key(&store, &root).unwrap_err();
        assert!(error.contains("No new key was created") && error.contains("fixture recovery"));
        assert_eq!(store.writes.load(Ordering::SeqCst), 0);
        // Damaged credential: rejected, never replaced.
        let store = memory_key(Some(""));
        assert!(resolve_master_key(&store, &root)
            .unwrap_err()
            .contains("empty or damaged"));
        assert_eq!(store.writes.load(Ordering::SeqCst), 0);
        std::fs::remove_dir_all(root).unwrap();
        // Fresh install: exactly one key is created and a marker records it.
        let fresh = temp_root("keychain-fresh");
        std::fs::create_dir_all(fresh.join("encrypted-cache")).unwrap();
        let store = memory_key(None);
        let key = resolve_master_key(&store, &fresh).unwrap();
        assert_eq!(resolve_master_key(&store, &fresh).unwrap(), key);
        assert_eq!(store.writes.load(Ordering::SeqCst), 1);
        assert!(fresh.join(MASTER_KEY_MARKER).exists());
        // Losing the credential after creation is detected, not silently replaced.
        let lost = memory_key(None);
        assert!(resolve_master_key(&lost, &fresh).is_err());
        assert_eq!(lost.writes.load(Ordering::SeqCst), 0);
        std::fs::remove_dir_all(fresh).unwrap();
    }

    #[test]
    fn concurrent_first_credential_requests_create_one_key() {
        let root = temp_root("keychain-race");
        let store = Arc::new(memory_key(None));
        let barrier = Arc::new(std::sync::Barrier::new(8));
        let workers: Vec<_> = (0..8)
            .map(|_| {
                let (root, store, barrier) = (root.clone(), store.clone(), barrier.clone());
                std::thread::spawn(move || {
                    barrier.wait();
                    resolve_master_key(store.as_ref(), &root).unwrap()
                })
            })
            .collect();
        let keys: Vec<_> = workers.into_iter().map(|w| w.join().unwrap()).collect();
        assert!(keys.iter().all(|k| k == &keys[0]));
        assert_eq!(store.writes.load(Ordering::SeqCst), 1);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn private_files_publish_atomically_without_clobbering_keys() {
        let root = temp_root("atomic");
        let path = root.join("simulation-master");
        write_private_file(&path, b"first-value", false).unwrap();
        assert!(write_private_file(&path, b"second-value", false).is_err());
        assert_eq!(std::fs::read(&path).unwrap(), b"first-value");
        let cache = root.join("optouts-t.enc");
        write_private_file(&cache, b"one", true).unwrap();
        write_private_file(&cache, b"two", true).unwrap();
        assert_eq!(std::fs::read(&cache).unwrap(), b"two");
        assert!(temporary_files(&root).is_empty());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            for file in [&path, &cache] {
                let mode = std::fs::metadata(file).unwrap().permissions().mode();
                assert_eq!(mode & 0o777, 0o600);
            }
        }
        std::fs::remove_dir_all(root).unwrap();
    }

    fn shell_app() -> tauri::App<tauri::test::MockRuntime> {
        tauri::test::mock_builder()
            .plugin(tauri_plugin_shell::init())
            .build(tauri::test::mock_context(tauri::test::noop_assets()))
            .unwrap()
    }

    fn owned_backend(child: CommandChild, generation: u64, exited: Arc<AtomicBool>) -> Backend {
        Backend {
            config: BackendConfig {
                url: "http://127.0.0.1:9".into(),
                local_token: "fixture".into(),
                simulation: false,
            },
            child,
            generation,
            exited,
        }
    }

    #[cfg(unix)]
    #[tokio::test(flavor = "current_thread")]
    async fn exited_authority_is_forgotten_and_a_lock_refusal_is_reported() {
        let app = shell_app();
        let (events, child) = managed_sidecar(app.shell().command("sh").args(["-c", "exit 75"]))
            .spawn()
            .unwrap();
        let local = LocalBackend::default();
        let exited = Arc::new(AtomicBool::new(false));
        local.0.lock().unwrap().backend = Some(owned_backend(child, 7, exited.clone()));
        assert!(local.current().unwrap().is_some());
        drain_backend(
            events,
            local.clone(),
            WebhookTunnel::default(),
            7,
            exited.clone(),
        )
        .await;
        assert!(exited.load(Ordering::SeqCst));
        assert!(
            local.current().unwrap().is_none(),
            "a dead authority is never reused"
        );
        let state = local.0.lock().unwrap();
        assert!(state.backend.is_none());
        assert_eq!(state.refused, Some(false));
    }

    #[cfg(unix)]
    #[tokio::test(flavor = "current_thread")]
    async fn a_stale_drain_does_not_forget_the_replacement_authority() {
        let app = shell_app();
        let (old_events, old_child) = managed_sidecar(app.shell().command("true"))
            .spawn()
            .unwrap();
        let (_events, current) = managed_sidecar(app.shell().command("sleep").args(["30"]))
            .spawn()
            .unwrap();
        let _ = old_child.kill();
        let local = LocalBackend::default();
        local.0.lock().unwrap().backend =
            Some(owned_backend(current, 2, Arc::new(AtomicBool::new(false))));
        drain_backend(
            old_events,
            local.clone(),
            WebhookTunnel::default(),
            1,
            Arc::new(AtomicBool::new(false)),
        )
        .await;
        assert!(local.current().unwrap().is_some());
        let backend = local.take().unwrap().unwrap();
        backend.child.kill().unwrap();
    }

    #[cfg(unix)]
    #[tokio::test(flavor = "current_thread")]
    async fn updater_cleanup_waits_for_the_owned_authority_group_to_exit() {
        let app = shell_app();
        let (mut events, child) = managed_sidecar(
            app.shell()
                .command("sh")
                .args(["-c", "sleep 30 & echo $!; wait"]),
        )
        .spawn()
        .unwrap();
        let helper = loop {
            if let Some(CommandEvent::Stdout(bytes)) = events.recv().await {
                break String::from_utf8(bytes)
                    .unwrap()
                    .trim()
                    .parse::<u32>()
                    .unwrap();
            }
        };
        let leader = child.pid();
        let local = LocalBackend::default();
        local.0.lock().unwrap().backend =
            Some(owned_backend(child, 1, Arc::new(AtomicBool::new(false))));
        let cleanup = local.clone();
        register_exit_cleanup(app.handle(), move || {
            stop_owned_runtimes(
                &NativeAuthority::default(),
                &WebhookTunnel::default(),
                &cleanup,
            )
        });
        app.cleanup_before_exit();
        let alive = |pid: u32| {
            std::process::Command::new("kill")
                .args(["-0", &pid.to_string()])
                .stderr(std::process::Stdio::null())
                .status()
                .unwrap()
                .success()
        };
        // No grace period: the leader is reaped before cleanup returns to the updater.
        assert!(
            !alive(leader),
            "installer must not start while the authority runs"
        );
        assert!(local.current().unwrap().is_none());
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
        assert!(
            !survived,
            "the frozen runtime child must not outlive cleanup"
        );
    }
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

    fn temp_root(label: &str) -> std::path::PathBuf {
        let root = std::env::temp_dir().join(format!("opendialer-{label}-{}", Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        root
    }

    fn temporary_files(root: &std::path::Path) -> Vec<String> {
        std::fs::read_dir(root)
            .unwrap()
            .filter_map(|e| e.ok())
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .filter(|name| name.ends_with(".tmp"))
            .collect()
    }

    #[test]
    fn simulation_key_rejects_an_empty_or_damaged_file_without_replacing_it() {
        let root = temp_root("key-empty");
        std::fs::write(root.join("simulation-master"), b"").unwrap();
        let error = master_key(true, &root).unwrap_err();
        assert!(error.contains("empty or damaged"), "{error}");
        assert_eq!(std::fs::read(root.join("simulation-master")).unwrap(), b"");
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn missing_key_is_never_reminted_over_existing_encrypted_data() {
        let root = temp_root("key-existing");
        std::fs::create_dir_all(root.join("encrypted-cache")).unwrap();
        std::fs::write(root.join("encrypted-cache/optouts-t.enc"), b"ODC1 fixture").unwrap();
        let error = master_key(true, &root).unwrap_err();
        assert!(error.contains("No new key was created"), "{error}");
        assert!(!root.join("simulation-master").exists());
        std::fs::remove_dir_all(root).unwrap();

        // An empty cache directory and an interrupted temporary write are not protected data.
        let fresh = temp_root("key-fresh");
        std::fs::create_dir_all(fresh.join("encrypted-cache")).unwrap();
        std::fs::write(fresh.join("simulation-master.0000.tmp"), b"partial").unwrap();
        let key = master_key(true, &fresh).unwrap();
        assert!(key.len() >= 32);
        assert_eq!(master_key(true, &fresh).unwrap(), key);
        // Once a key was created here, losing it is reported even before other data exists.
        std::fs::remove_file(fresh.join("simulation-master")).unwrap();
        assert!(master_key(true, &fresh).is_err());
        assert!(!fresh.join("simulation-master").exists());
        std::fs::remove_dir_all(fresh).unwrap();
    }

    #[test]
    fn concurrent_first_key_requests_share_one_published_key() {
        let root = temp_root("key-race");
        let barrier = Arc::new(std::sync::Barrier::new(8));
        let workers: Vec<_> = (0..8)
            .map(|_| {
                let (root, barrier) = (root.clone(), barrier.clone());
                std::thread::spawn(move || {
                    barrier.wait();
                    master_key(true, &root)
                })
            })
            .collect();
        let keys: Vec<_> = workers.into_iter().map(|w| w.join().unwrap()).collect();
        assert!(keys.iter().all(|k| k.is_ok()), "{keys:?}");
        let first = keys[0].clone().unwrap();
        assert!(first.len() >= 32);
        assert!(
            keys.iter().all(|k| k.as_ref().unwrap() == &first),
            "{keys:?}"
        );
        assert_eq!(
            std::fs::read_to_string(root.join("simulation-master")).unwrap(),
            first
        );
        assert!(temporary_files(&root).is_empty());
        std::fs::remove_dir_all(root).unwrap();
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

/// Where one mode's data key lives. `load` returns `None` only when the key is absent.
trait MasterKeyStore {
    fn load(&self) -> Result<Option<String>, String>;
    fn create(&self, value: &str) -> Result<(), String>;
    fn recovery(&self) -> &'static str;
}

struct CredentialStoreKey(keyring::Entry);
impl MasterKeyStore for CredentialStoreKey {
    fn load(&self) -> Result<Option<String>, String> {
        match self.0.get_password() {
            Ok(value) => Ok(Some(value)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(_) => Err("Unlock your OS credential store to start OpenDialer".into()),
        }
    }
    fn create(&self, value: &str) -> Result<(), String> {
        self.0
            .set_password(value)
            .map_err(|_| "Cannot protect the local authority key in the OS credential store".into())
    }
    fn recovery(&self) -> &'static str {
        "Restore the OpenDialer credential (service com.opendialer.desktop, account authority-master-v1) to your OS credential store from a backup, then reopen OpenDialer"
    }
}

struct FileKey(std::path::PathBuf);
impl MasterKeyStore for FileKey {
    fn load(&self) -> Result<Option<String>, String> {
        match std::fs::read_to_string(&self.0) {
            Ok(value) => Ok(Some(value)),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(_) => Err("Cannot read the simulation data key".into()),
        }
    }
    fn create(&self, value: &str) -> Result<(), String> {
        write_private_file(&self.0, value.as_bytes(), false)
    }
    fn recovery(&self) -> &'static str {
        "Simulation data is synthetic: quit OpenDialer and move the simulation folder aside to start a fresh simulation, or restore simulation-master from a backup"
    }
}

const MASTER_KEY_MARKER: &str = "master-key-created";
const MASTER_KEY_LOCK: &str = "master-key.lock";
static MASTER_KEY_CREATION: Mutex<()> = Mutex::new(());

fn checked_master_key(store: &dyn MasterKeyStore, value: String) -> Result<String, String> {
    if (32..=4096).contains(&value.len()) && value.bytes().all(|b| b.is_ascii_graphic()) {
        return Ok(value);
    }
    Err(format!(
        "The stored local data key is empty or damaged and was not replaced. {}.",
        store.recovery()
    ))
}

/// Anything beyond an empty cache directory, lock files and interrupted temporary writes
/// may be encrypted with the missing key.
fn protected_data_present(directory: &std::path::Path) -> Result<bool, String> {
    let entries = match std::fs::read_dir(directory) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(_) => return Err("Cannot inspect protected application data".into()),
    };
    for entry in entries {
        let entry = entry.map_err(|_| "Cannot inspect protected application data")?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.ends_with(".tmp")
            || matches!(
                name.as_ref(),
                MASTER_KEY_LOCK | "authority.lock" | ".DS_Store" | "desktop.ini" | "Thumbs.db"
            )
        {
            continue;
        }
        if name == "encrypted-cache"
            && entry.file_type().is_ok_and(|kind| kind.is_dir())
            && std::fs::read_dir(entry.path())
                .map_err(|_| "Cannot inspect protected application data")?
                .next()
                .is_none()
        {
            continue;
        }
        return Ok(true);
    }
    Ok(false)
}

/// Load the mode's data key, creating it only for a directory with no protected data.
/// First creation is serialized in this process and across desktop processes.
fn resolve_master_key(
    store: &dyn MasterKeyStore,
    directory: &std::path::Path,
) -> Result<String, String> {
    if let Some(value) = store.load()? {
        return checked_master_key(store, value);
    }
    let _serial = MASTER_KEY_CREATION
        .lock()
        .map_err(|_| "Protected key creation unavailable")?;
    std::fs::create_dir_all(directory).map_err(|_| "Cannot create private application data")?;
    let lock = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(directory.join(MASTER_KEY_LOCK))
        .map_err(|_| "Cannot serialize protected key creation")?;
    if let Err(error) = lock.lock() {
        if error.kind() != std::io::ErrorKind::Unsupported {
            return Err("Cannot serialize protected key creation".into());
        }
    }
    if let Some(value) = store.load()? {
        return checked_master_key(store, value);
    }
    if directory.join(MASTER_KEY_MARKER).exists() || protected_data_present(directory)? {
        return Err(format!(
            "OpenDialer found existing protected data but its key is missing. No new key was created, so the data is unchanged. {}.",
            store.recovery()
        ));
    }
    let value = format!("{}{}", Uuid::new_v4(), Uuid::new_v4());
    store.create(&value)?;
    if store.load()?.as_deref() != Some(value.as_str()) {
        return Err(
            "The new local data key could not be read back; nothing was encrypted with it".into(),
        );
    }
    write_private_file(&directory.join(MASTER_KEY_MARKER), b"1\n", true)?;
    Ok(value)
}

fn master_key(simulation: bool, directory: &std::path::Path) -> Result<String, String> {
    if simulation {
        return resolve_master_key(&FileKey(directory.join("simulation-master")), directory);
    }
    resolve_master_key(
        &CredentialStoreKey(secret_entry("authority-master-v1")?),
        directory,
    )
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
    let native = app.state::<NativeAuthority>();
    guard_local_mode(native.binding()?.as_ref(), simulation)?;
    if let Some(existing) = state.current()?.filter(|c| c.simulation == simulation) {
        return Ok(existing);
    }
    let directory = app
        .path()
        .app_data_dir()
        .map_err(|_| "App data directory unavailable")?
        .join(if simulation { "simulation" } else { "local" });
    std::fs::create_dir_all(&directory).map_err(|_| "Cannot create private application data")?;
    // The credential store may show an OS prompt; no shared lock is held while it does.
    let secret_key = master_key(simulation, &directory)?;
    let authority = native.0.lock().map_err(|_| "Workspace unavailable")?;
    guard_local_mode(authority.binding.as_ref(), simulation)?;
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
    // Secrets travel over the child's private stdin pipe, never through its environment.
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
    if child.write(&launch_secrets(&nonce, &secret_key)?).is_err() {
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
    purge_legacy_after_healthy(config.url.clone(), directory, exited.clone());
    locked.backend = Some(Backend {
        config: config.clone(),
        child,
        generation,
        exited,
    });
    drop(locked);
    drop(authority);
    Ok(config)
}

/// One JSON line, read once by the authority (`--secrets-stdin`).
fn launch_secrets(local_token: &str, secret_key: &str) -> Result<Vec<u8>, String> {
    let mut line = serde_json::to_vec(&serde_json::json!({
        "local_token": local_token,
        "secret_key": secret_key,
    }))
    .map_err(|_| "Cannot prepare local service secrets")?;
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

const LEGACY_PURGE_MARKER: &str = "legacy-cache-purge-v1";

/// The legacy raw-cache cutover runs once per mode, only after its authority is healthy
/// (migrations and backups done), and can never abort startup.
fn purge_legacy_after_healthy(url: String, root: std::path::PathBuf, exited: Arc<AtomicBool>) {
    if root.join(LEGACY_PURGE_MARKER).exists() {
        return;
    }
    std::thread::spawn(move || {
        let deadline = std::time::Instant::now() + Duration::from_secs(300);
        while std::time::Instant::now() < deadline && !exited.load(Ordering::SeqCst) {
            if local_backend_healthy(&url) {
                // A failure is retried on a later launch because no marker is written.
                let _ = purge_legacy_caches_once(&root);
                return;
            }
            std::thread::sleep(Duration::from_millis(500));
        }
    });
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
        if value.len() > 2_000_000 {
            return Err("Cache exceeds the bounded storage limit".into());
        }
        let (root, path) = cache_path(&app, &namespace, simulation)?;
        let secret = master_key(simulation, &root)?;
        // The opt-out journal uses this path: the rename is durable only after the
        // directory entry itself is flushed.
        write_private_file(
            &path,
            &encrypt_blob(&secret, &namespace, value.as_bytes())?,
            true,
        )
        .map_err(|_| "Cannot publish cache atomically".into())
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

/// Run the legacy cutover for one mode directory at most once (marker written on success).
pub fn purge_legacy_caches_once(root: &std::path::Path) -> Result<Option<usize>, String> {
    let marker = root.join(LEGACY_PURGE_MARKER);
    if marker.exists() {
        return Ok(None);
    }
    let removed = purge_legacy_caches(&root.join("encrypted-cache"))?;
    write_private_file(&marker, b"1\n", true)?;
    Ok(Some(removed))
}

/// Quit, mode change and updater cleanup: stop the owned SSH child and wait for it, then
/// terminate the authority's process group (Unix) or job object (Windows) and wait for it.
fn stop_owned_runtimes(
    authority: &NativeAuthority,
    tunnel: &WebhookTunnel,
    backend: &LocalBackend,
) {
    authority.clear();
    let _ = tunnel.stop();
    if let Ok(Some(owned)) = backend.take() {
        let _ = owned.child.kill();
        #[cfg(windows)]
        wait_for_backend_release(Duration::from_secs(10));
    }
}

/// The updater's installer replaces opendialer-backend.exe right after cleanup. A job member
/// that is still terminating keeps the image mapped, so wait until it can be opened exclusively.
#[cfg(windows)]
fn wait_for_backend_release(limit: Duration) {
    use std::os::windows::fs::OpenOptionsExt;
    let Some(path) = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|dir| dir.join("opendialer-backend.exe")))
    else {
        return;
    };
    let deadline = std::time::Instant::now() + limit;
    while path.is_file() && std::time::Instant::now() < deadline {
        match std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .share_mode(0)
            .open(&path)
        {
            Ok(_) => return,
            // ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION while a process still uses it.
            Err(error) if matches!(error.raw_os_error(), Some(32 | 33)) => {
                std::thread::sleep(Duration::from_millis(100))
            }
            Err(_) => return,
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
        .manage(NativeAuthority::default())
        .setup(|app| {
            // tauri-plugin-updater's own on_before_exit hook calls cleanup_before_exit() before
            // it launches the NSIS or MSI installer and calls process::exit(0); that clears the
            // resource table, which runs this guard. Legacy cache cleanup is deferred until the
            // authority is healthy (purge_legacy_after_healthy) and never aborts setup.
            let backend = app.state::<LocalBackend>().inner().clone();
            let tunnel = app.state::<WebhookTunnel>().inner().clone();
            let authority = app.state::<NativeAuthority>().inner().clone();
            register_exit_cleanup(app.handle(), move || {
                stop_owned_runtimes(&authority, &tunnel, &backend)
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
            stop_owned_runtimes(
                &handle.state::<NativeAuthority>(),
                &handle.state::<WebhookTunnel>(),
                &handle.state::<LocalBackend>(),
            );
        }
    });
}
