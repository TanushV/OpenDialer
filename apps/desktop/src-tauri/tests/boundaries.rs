use opendialer_lib::validate_backend_url;

#[test]
fn connected_authority_must_be_https_except_explicit_loopback_development() {
    assert!(validate_backend_url("https://dialer.example.com").is_ok());
    assert!(validate_backend_url("http://127.0.0.1:8765").is_ok());
    assert!(validate_backend_url("http://evil.example.com").is_err());
    assert!(validate_backend_url("javascript:alert(1)").is_err());
    assert!(validate_backend_url("https://user:password@example.com").is_err());
}
