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
    let command = app
        .shell()
        .sidecar("opendialer-backend")
        .map_err(|_| "Bundled local service is missing")?
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

pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_opener::init())
        .manage(LocalBackend::default())
        .invoke_handler(tauri::generate_handler![
            backend_config,
            save_session,
            load_session,
            clear_session,
            check_server_url,
            open_source
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
