#[cfg(unix)]
#[tokio::test(flavor = "current_thread")]
async fn sidecar_exit_also_terminates_the_frozen_runtime_child() {
    use tauri_plugin_shell::{process::CommandEvent, ShellExt};
    let app = tauri::test::mock_builder()
        .plugin(tauri_plugin_shell::init())
        .build(tauri::test::mock_context(tauri::test::noop_assets()))
        .unwrap();
    let command = opendialer_lib::managed_sidecar(
        app.shell()
            .command("sh")
            .args(["-c", "sleep 30 & echo $!; wait"]),
    );
    let (mut events, process) = command.spawn().unwrap();
    let child_pid = loop {
        if let Some(CommandEvent::Stdout(bytes)) = events.recv().await {
            break String::from_utf8(bytes)
                .unwrap()
                .trim()
                .parse::<u32>()
                .unwrap();
        }
    };
    process.kill().unwrap();
    tokio::time::sleep(std::time::Duration::from_millis(150)).await;
    let still_alive = std::process::Command::new("kill")
        .args(["-0", &child_pid.to_string()])
        .output()
        .unwrap()
        .status
        .success();
    if still_alive {
        let _ = std::process::Command::new("kill")
            .arg(child_pid.to_string())
            .status();
    }
    assert!(
        !still_alive,
        "a packaged child must not outlive the authority launcher"
    );
}

#[test]
fn updater_cleanup_releases_owned_runtime_without_a_run_event() {
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    };
    let closed = Arc::new(AtomicBool::new(false));
    let effect = closed.clone();
    let app = tauri::test::mock_builder()
        .build(tauri::test::mock_context(tauri::test::noop_assets()))
        .unwrap();
    opendialer_lib::register_exit_cleanup(app.handle(), move || {
        effect.store(true, Ordering::SeqCst);
    });
    assert!(!closed.load(Ordering::SeqCst));
    app.cleanup_before_exit();
    assert!(
        closed.load(Ordering::SeqCst),
        "Windows updater cleanup must stop owned runtimes before process::exit"
    );
}
