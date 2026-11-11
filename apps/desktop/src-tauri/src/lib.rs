use serde::Serialize;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{Emitter, Manager, State};
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};
use uuid::Uuid;

/// Exit status of the Python authority when another authority owns its data directory.
const AUTHORITY_BUSY_EXIT: i32 = 75;
/// Exit status when the authority could not move older data into this version's folder (R3.9).
const MIGRATION_FAILED_EXIT: i32 = 78;
/// This version's data folder, and the older one it is copied from on first launch (R3.7).
const DATA_FOLDER: &str = "local-3";
const PREVIOUS_DATA_FOLDER: &str = "local-2";
const MIGRATION_NOTE: &str = "migration-failed.txt";

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
    /// The last authority stopped because moving older data failed; the next start says why, then retries.
    migration_failed: bool,
    /// Set while the app quits or updates, so an exiting authority is not restarted (R4.10).
    stopping: bool,
    /// When the authority was last restarted after an unexpected exit.
    restarts: Vec<Instant>,
}

/// At most this many automatic restarts within `RESTART_WINDOW`; after that the next request starts it.
const RESTART_LIMIT: usize = 5;
const RESTART_WINDOW: Duration = Duration::from_secs(60);

/// Whether an exit that nobody asked for should start the authority again (G7).
fn should_restart(code: Option<i32>, stopping: bool, recent: usize) -> bool {
    !stopping
        && recent < RESTART_LIMIT
        && code != Some(AUTHORITY_BUSY_EXIT)
        && code != Some(MIGRATION_FAILED_EXIT)
        && code != Some(0)
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
    /// The exited authority, and the delay before restarting it when the exit was unexpected.
    fn forget_exited(
        &self,
        generation: u64,
        code: Option<i32>,
    ) -> Option<(Backend, Option<Duration>)> {
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
        if code == Some(MIGRATION_FAILED_EXIT) {
            state.migration_failed = true;
        }
        let now = Instant::now();
        state
            .restarts
            .retain(|at| now.duration_since(*at) < RESTART_WINDOW);
        let recent = state.restarts.len();
        let restart = should_restart(code, state.stopping, recent).then(|| {
            state.restarts.push(now);
            Duration::from_millis(500 * (1 << recent.min(4)))
        });
        Some((backend, restart))
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
    // Store format 3: the authority copies `local-2` into `local-3` on first launch and never
    // changes `local-2`, which stays the rollback for the previous version.
    let base = app
        .path()
        .app_data_dir()
        .map_err(|_| "App data directory unavailable")?;
    let directory = base.join(if simulation {
        "simulation-2"
    } else {
        DATA_FOLDER
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
    if !simulation && std::mem::take(&mut locked.migration_failed) {
        return Err(migration_failure(&directory));
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
        "--port".into(),
        port.to_string(),
        "--data-dir".into(),
        directory.to_string_lossy().to_string(),
        "--secrets-stdin".into(),
    ];
    if simulation {
        args.push("--simulation".into());
    } else {
        args.push("--migrate-from".into());
        args.push(
            base.join(PREVIOUS_DATA_FOLDER)
                .to_string_lossy()
                .to_string(),
        );
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
        app.clone(),
        receiver,
        state.clone(),
        generation,
        exited.clone(),
        simulation,
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

/// What the rep sees when moving older data failed: the reason from the authority's note, and that nothing was lost.
fn migration_failure(directory: &std::path::Path) -> String {
    let reason = std::fs::read_to_string(directory.join(MIGRATION_NOTE))
        .map(|note| note.trim().to_string())
        .unwrap_or_default();
    let mut message = format!(
        "OpenDialer could not move your data to this version. Your data in {PREVIOUS_DATA_FOLDER} is unchanged; reinstall the previous version to keep calling, or retry."
    );
    if !reason.is_empty() {
        message.push_str(" Reason: ");
        message.push_str(&reason);
    }
    message
}

/// Drain without forwarding provider or user content into ordinary desktop logs. When the
/// owned child exits unexpectedly, start it again and tell the window its new address (R4.10).
async fn drain_backend(
    app: tauri::AppHandle,
    mut events: tauri::async_runtime::Receiver<CommandEvent>,
    local: LocalBackend,
    generation: u64,
    exited: Arc<AtomicBool>,
    simulation: bool,
) {
    let mut code = None;
    while let Some(event) = events.recv().await {
        if let CommandEvent::Terminated(payload) = event {
            code = payload.code;
        }
    }
    exited.store(true, Ordering::SeqCst);
    let _ = tauri::async_runtime::spawn_blocking(move || {
        let Some((backend, restart)) = local.forget_exited(generation, code) else {
            return;
        };
        // Reap any remaining group members; the leader has already exited.
        let _ = backend.child.kill();
        let Some(delay) = restart else {
            return;
        };
        std::thread::sleep(delay);
        if let Ok(config) = start_backend(&app, &local, simulation) {
            let _ = app.emit("backend-restarted", config);
        }
    })
    .await;
}

/// Keeps the computer awake while the rep is Ready, so it does not sleep mid-session (R4.11, R9D.2, G8).
#[derive(Default)]
struct KeepAwake(Mutex<Option<awake::Hold>>);

/// macOS: `caffeinate` keeps the system and display awake and exits by itself if this app does.
#[cfg(target_os = "macos")]
mod awake {
    pub fn arguments(pid: u32) -> Vec<String> {
        vec!["-d".into(), "-i".into(), "-w".into(), pid.to_string()]
    }

    pub struct Hold(std::process::Child);

    impl Hold {
        pub fn start() -> Result<Self, String> {
            std::process::Command::new("/usr/bin/caffeinate")
                .args(arguments(std::process::id()))
                .spawn()
                .map(Hold)
                .map_err(|_| "Cannot keep this Mac awake".to_string())
        }
        pub fn alive(&mut self) -> bool {
            matches!(self.0.try_wait(), Ok(None))
        }
        pub fn stop(mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}

/// Windows: one thread holds `SetThreadExecutionState` for the system and display until it is told to stop; the
/// request ends with that thread, and with the app.
#[cfg(windows)]
mod awake {
    use std::sync::mpsc;
    use windows_sys::Win32::System::Power::{
        SetThreadExecutionState, ES_CONTINUOUS, ES_DISPLAY_REQUIRED, ES_SYSTEM_REQUIRED,
    };

    pub const AWAKE: u32 = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED;

    pub struct Hold {
        stop: mpsc::Sender<()>,
        thread: std::thread::JoinHandle<()>,
    }

    impl Hold {
        pub fn start() -> Result<Self, String> {
            let (stop, wait) = mpsc::channel::<()>();
            let (ready, started) = mpsc::channel::<bool>();
            let thread = std::thread::spawn(move || {
                // SAFETY: a plain Win32 call with documented flag values; it only affects this thread's request.
                let held = unsafe { SetThreadExecutionState(AWAKE) } != 0;
                let _ = ready.send(held);
                let _ = wait.recv();
                unsafe { SetThreadExecutionState(ES_CONTINUOUS) };
            });
            match started.recv() {
                Ok(true) => Ok(Hold { stop, thread }),
                _ => Err("Cannot keep this PC awake".to_string()),
            }
        }
        pub fn alive(&mut self) -> bool {
            !self.thread.is_finished()
        }
        pub fn stop(self) {
            let _ = self.stop.send(());
            let _ = self.thread.join();
        }
    }
}

#[cfg(not(any(target_os = "macos", windows)))]
mod awake {
    pub struct Hold;
    impl Hold {
        pub fn start() -> Result<Self, String> {
            Err("Keep-awake is not supported on this system".to_string())
        }
        pub fn alive(&mut self) -> bool {
            false
        }
        pub fn stop(self) {}
    }
}

#[tauri::command]
fn keep_awake(state: State<'_, KeepAwake>, on: bool) -> Result<bool, String> {
    let mut held = state.0.lock().map_err(|_| "Keep-awake state unavailable")?;
    if let Some(hold) = held.as_mut() {
        if !hold.alive() {
            *held = None; // It ended on its own; start a new one if still wanted.
        }
    }
    match (on, held.is_some()) {
        (true, false) => *held = Some(awake::Hold::start()?),
        (false, true) => {
            if let Some(hold) = held.take() {
                hold.stop();
            }
        }
        _ => {}
    }
    Ok(held.is_some())
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

/// Quit and updater cleanup: terminate the authority's process group, and never restart it.
fn stop_owned_runtimes(backend: &LocalBackend) {
    if let Ok(mut state) = backend.0.lock() {
        state.stopping = true;
    }
    if let Ok(Some(owned)) = backend.take() {
        let _ = owned.child.kill();
        #[cfg(windows)]
        wait_for_backend_release(std::time::Duration::from_secs(10));
    }
}

/// The updater's installer replaces opendialer-backend.exe and opendialer-trainer.exe right after cleanup. A child
/// still terminating keeps its image mapped, so wait until each executable can be opened exclusively, within one
/// shared limit (the Windows update lesson from 0.3.3).
#[cfg(windows)]
fn wait_for_backend_release(limit: std::time::Duration) {
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
                Err(_) => std::thread::sleep(std::time::Duration::from_millis(100)),
            }
        }
    }
}

#[cfg(target_os = "macos")]
struct CallbackActivity(
    objc2::rc::Retained<objc2::runtime::ProtocolObject<dyn objc2_foundation::NSObjectProtocol>>,
);

#[cfg(target_os = "macos")]
impl CallbackActivity {
    fn begin() -> Self {
        use objc2_foundation::{NSActivityOptions, NSProcessInfo, NSString};
        Self(
            NSProcessInfo::processInfo().beginActivityWithOptions_reason(
                NSActivityOptions::UserInitiatedAllowingIdleSystemSleep,
                &NSString::from_str("Receive OpenDialer callbacks while the app is open"),
            ),
        )
    }
}

#[cfg(target_os = "macos")]
impl Drop for CallbackActivity {
    fn drop(&mut self) {
        // SAFETY: this token came from NSProcessInfo's own beginActivity method.
        unsafe { objc2_foundation::NSProcessInfo::processInfo().endActivity(&self.0) };
    }
}

pub fn run() {
    #[cfg(target_os = "macos")]
    let _callback_activity = CallbackActivity::begin();
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_opener::init())
        .manage(LocalBackend::default())
        .manage(KeepAwake::default())
        .setup(|app| {
            // The updater clears the resource table before it launches an installer and exits,
            // which runs this guard.
            let backend = app.state::<LocalBackend>().inner().clone();
            register_exit_cleanup(app.handle(), move || stop_owned_runtimes(&backend));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            backend_config,
            keep_awake,
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_failed_migration_names_its_reason_and_that_data_is_unchanged() {
        let folder = std::env::temp_dir().join(format!("opendialer-migration-{}", Uuid::new_v4()));
        std::fs::create_dir_all(&folder).unwrap();
        let bare = migration_failure(&folder);
        assert!(bare.contains("local-2 is unchanged") && !bare.contains("Reason"));
        std::fs::write(folder.join(MIGRATION_NOTE), "disk full\n").unwrap();
        assert!(migration_failure(&folder).ends_with("Reason: disk full"));
        std::fs::remove_dir_all(folder).unwrap();
    }

    #[test]
    fn only_an_unexpected_exit_restarts_the_authority_and_not_forever() {
        assert!(should_restart(None, false, 0));
        assert!(should_restart(Some(1), false, RESTART_LIMIT - 1));
        assert!(!should_restart(Some(1), false, RESTART_LIMIT));
        assert!(!should_restart(None, true, 0));
        assert!(!should_restart(Some(0), false, 0));
        assert!(!should_restart(Some(AUTHORITY_BUSY_EXIT), false, 0));
        assert!(!should_restart(Some(MIGRATION_FAILED_EXIT), false, 0));
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn keep_awake_holds_system_and_display_and_ends_with_the_app() {
        assert_eq!(awake::arguments(42), ["-d", "-i", "-w", "42"]);
    }

    #[test]
    #[cfg(windows)]
    fn keep_awake_holds_system_and_display_until_released() {
        use windows_sys::Win32::System::Power::{
            ES_CONTINUOUS, ES_DISPLAY_REQUIRED, ES_SYSTEM_REQUIRED,
        };
        assert_eq!(
            awake::AWAKE,
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        );
        let mut hold = awake::Hold::start().expect("SetThreadExecutionState");
        assert!(hold.alive());
        hold.stop();
    }
}
