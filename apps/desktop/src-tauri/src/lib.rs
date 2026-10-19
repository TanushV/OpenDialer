use serde::Serialize;
use std::sync::{Arc, Mutex};
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
        let error = spawn_webhook_ssh("/missing/opendialer-ssh-fixture", &["-NT".into()]).unwrap_err();
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
            std::process::Command::new("false").spawn().map(OwnedWebhookTunnel::new)
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
    secure_io(move || start_backend(app, backend, simulation)).await
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
        .setup(|app| {
            let root = app.path().app_data_dir()?;
            for mode in ["local", "simulation"] {
                purge_legacy_caches(&root.join(mode).join("encrypted-cache"))
                    .map_err(std::io::Error::other)?;
            }
            let backend = app.state::<LocalBackend>().inner().clone();
            register_exit_cleanup(app.handle(), move || {
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
            cache_write
        ])
        .build(tauri::generate_context!())
        .expect("OpenDialer could not start");
    app.run(|handle, event| {
        if let tauri::RunEvent::Exit = event {
            if let Ok(mut state) = handle.state::<LocalBackend>().0.lock() {
                if let Some(backend) = state.take() {
                    let _ = backend.child.kill();
                }
            }
        }
    });
}
