use serde_json::{json, Value};
use std::env;
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::Manager;

#[cfg(unix)]
use std::io::{BufRead, BufReader, Write};
#[cfg(not(unix))]
use std::io::{BufRead, BufReader, Write};
#[cfg(unix)]
use std::os::unix::net::UnixStream;
#[cfg(unix)]
use std::path::PathBuf;
#[cfg(not(unix))]
use std::net::TcpStream;

fn request_id() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_nanos())
        .unwrap_or_default();
    format!("tauri-{nanos}")
}

#[cfg(unix)]
fn kestrel_home() -> Result<PathBuf, String> {
    if let Ok(value) = env::var("KESTREL_HOME") {
        return Ok(PathBuf::from(value));
    }
    let home = env::var("HOME").map_err(|_| "HOME is not set".to_string())?;
    Ok(PathBuf::from(home).join(".kestrel"))
}

#[cfg(unix)]
fn send_control_request(method: &str, params: Value) -> Result<Value, String> {
    let socket_path = kestrel_home()?.join("run").join("control.sock");
    let mut stream = UnixStream::connect(&socket_path)
        .map_err(|err| format!("Failed to connect to {}: {}", socket_path.display(), err))?;
    let request_id = request_id();
    let request = json!({
        "request_id": request_id,
        "method": method,
        "params": params,
    });
    let payload = serde_json::to_vec(&request).map_err(|err| err.to_string())?;
    stream.write_all(&payload).map_err(|err| err.to_string())?;
    stream.write_all(b"\n").map_err(|err| err.to_string())?;
    stream.flush().map_err(|err| err.to_string())?;

    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    loop {
        line.clear();
        let bytes = reader.read_line(&mut line).map_err(|err| err.to_string())?;
        if bytes == 0 {
            return Err(format!("Daemon closed the connection before completing {}", method));
        }

        let response: Value = serde_json::from_str(line.trim_end()).map_err(|err| err.to_string())?;
        if response
            .get("request_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            != request_id
        {
            continue;
        }

        if !response.get("ok").and_then(Value::as_bool).unwrap_or(false) {
            let message = response
                .get("error")
                .and_then(|error| error.get("message"))
                .and_then(Value::as_str)
                .unwrap_or("Unknown control API failure");
            return Err(message.to_string());
        }

        if let Some(result) = response.get("result") {
            return Ok(result.clone());
        }

        if response.get("done").and_then(Value::as_bool).unwrap_or(false) {
            return Ok(json!({}));
        }
    }
}

#[cfg(not(unix))]
fn send_control_request(method: &str, params: Value) -> Result<Value, String> {
    let host = env::var("KESTREL_CONTROL_HOST").unwrap_or_else(|_| "127.0.0.1".to_string());
    let port = env::var("KESTREL_CONTROL_PORT").unwrap_or_else(|_| "8749".to_string());
    let address = format!("{host}:{port}");
    let mut stream =
        TcpStream::connect(&address).map_err(|err| format!("Failed to connect to {}: {}", address, err))?;
    let request_id = request_id();
    let request = json!({
        "request_id": request_id,
        "method": method,
        "params": params,
    });
    let payload = serde_json::to_vec(&request).map_err(|err| err.to_string())?;
    stream.write_all(&payload).map_err(|err| err.to_string())?;
    stream.write_all(b"\n").map_err(|err| err.to_string())?;
    stream.flush().map_err(|err| err.to_string())?;

    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    loop {
        line.clear();
        let bytes = reader.read_line(&mut line).map_err(|err| err.to_string())?;
        if bytes == 0 {
            return Err(format!("Daemon closed the connection before completing {}", method));
        }

        let response: Value = serde_json::from_str(line.trim_end()).map_err(|err| err.to_string())?;
        if response
            .get("request_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            != request_id
        {
            continue;
        }

        if !response.get("ok").and_then(Value::as_bool).unwrap_or(false) {
            let message = response
                .get("error")
                .and_then(|error| error.get("message"))
                .and_then(Value::as_str)
                .unwrap_or("Unknown control API failure");
            return Err(message.to_string());
        }

        if let Some(result) = response.get("result") {
            return Ok(result.clone());
        }

        if response.get("done").and_then(Value::as_bool).unwrap_or(false) {
            return Ok(json!({}));
        }
    }
}

#[tauri::command]
fn daemon_status() -> Result<Value, String> {
    send_control_request("status", json!({}))
}

#[tauri::command]
fn daemon_doctor() -> Result<Value, String> {
    send_control_request("doctor", json!({}))
}

#[tauri::command]
fn daemon_runtime_profile() -> Result<Value, String> {
    send_control_request("runtime.profile", json!({}))
}

#[tauri::command]
fn daemon_sync_memory() -> Result<Value, String> {
    send_control_request("memory.sync", json!({}))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .invoke_handler(tauri::generate_handler![
            daemon_status,
            daemon_doctor,
            daemon_runtime_profile,
            daemon_sync_memory
        ])
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            let quit_i = tauri::menu::MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let hide_i = tauri::menu::MenuItem::with_id(app, "hide", "Hide", true, None::<&str>)?;
            let show_i = tauri::menu::MenuItem::with_id(app, "show", "Show", true, None::<&str>)?;
            let menu = tauri::menu::Menu::with_items(app, &[&show_i, &hide_i, &quit_i])?;

            let _tray = tauri::tray::TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .menu(&menu)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "quit" => {
                        app.exit(0);
                    }
                    "hide" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.hide();
                        }
                    }
                    "show" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let tauri::tray::TrayIconEvent::Click {
                        button: tauri::tray::MouseButton::Left,
                        button_state: tauri::tray::MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                })
                .build(app)?;

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
