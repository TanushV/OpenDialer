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
