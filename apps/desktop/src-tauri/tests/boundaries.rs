use opendialer_lib::validate_backend_url;

#[test]
fn connected_authority_must_be_https_except_explicit_loopback_development() {
    assert!(validate_backend_url("https://dialer.example.com").is_ok());
    assert!(validate_backend_url("http://127.0.0.1:8765").is_ok());
    assert!(validate_backend_url("http://evil.example.com").is_err());
    assert!(validate_backend_url("javascript:alert(1)").is_err());
    assert!(validate_backend_url("https://user:password@example.com").is_err());
}

#[tokio::test(flavor = "current_thread")]
async fn slow_credential_io_does_not_block_the_runtime() {
    use std::time::Duration;
    let operation = opendialer_lib::secure_io(|| {
        std::thread::sleep(Duration::from_millis(60));
        Ok::<_, String>("ready")
    });
    let timer = async {
        tokio::time::sleep(Duration::from_millis(5)).await;
        "responsive"
    };
    tokio::pin!(operation);
    tokio::pin!(timer);
    tokio::select! {
        value = &mut timer => assert_eq!(value, "responsive"),
        _ = &mut operation => panic!("Credential operation blocked the runtime timer"),
    }
    assert_eq!(operation.await.unwrap(), "ready");
}

#[test]
fn cached_contact_data_is_confidential_authenticated_and_bound_to_its_namespace() {
    let secret = "test-cache-secret";
    let text = br#"{"phone":"+14155550101","reason":"opt_out"}"#;
    let encrypted = opendialer_lib::encrypt_blob(secret, "tenant-a", text).unwrap();
    assert!(!encrypted.windows(12).any(|w| w == b"+14155550101"));
    assert_eq!(
        opendialer_lib::decrypt_blob(secret, "tenant-a", &encrypted).unwrap(),
        text
    );
    assert!(opendialer_lib::decrypt_blob(secret, "tenant-b", &encrypted).is_err());
    let mut tampered = encrypted.clone();
    let index = tampered.len() - 1;
    tampered[index] ^= 1;
    assert!(opendialer_lib::decrypt_blob(secret, "tenant-a", &tampered).is_err());
}

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
