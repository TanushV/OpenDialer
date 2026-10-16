use base64::{engine::general_purpose::STANDARD, Engine};
use minisign_verify::{PublicKey, Signature};
use std::{env, fs};

fn main() {
    let mut args = env::args().skip(1);
    let key_file = args.next().expect("Public key file required");
    let version = args.next().expect("Expected version required");
    let key_text = fs::read_to_string(key_file).unwrap();
    let public = String::from_utf8(STANDARD.decode(key_text.trim()).unwrap()).unwrap();
    let key = PublicKey::decode(&public).unwrap();
    for filename in args {
        let encoded = fs::read_to_string(format!("{filename}.sig")).unwrap();
        let decoded = String::from_utf8(STANDARD.decode(encoded.trim()).unwrap()).unwrap();
        let signature = Signature::decode(&decoded).unwrap();
        let mut data = fs::read(&filename).unwrap();
        key.verify(&data, &signature, true).unwrap();
        assert!(signature
            .trusted_comment()
            .split('\t')
            .any(|part| part == format!("version:{version}")));
        data[0] ^= 1;
        assert!(key.verify(&data, &signature, true).is_err());
        println!("PASS: pinned-key signature, authenticated version {version}, and tamper rejection for {}", std::path::Path::new(&filename).file_name().unwrap().to_string_lossy());
    }
}
