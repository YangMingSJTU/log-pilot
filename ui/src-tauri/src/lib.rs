use std::{
    io::Write,
    net::{TcpListener, TcpStream},
    process::{Child, Command, Stdio},
    sync::Mutex,
    thread,
    time::{Duration, Instant},
};

use serde::Serialize;
use tauri::{Manager, RunEvent, State};
use uuid::Uuid;

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct EngineConnection {
    base_url: String,
    token: String,
}

struct DesktopState {
    connection: EngineConnection,
    child: Mutex<Option<Child>>,
}

#[tauri::command]
fn engine_connection(state: State<'_, DesktopState>) -> EngineConnection {
    state.connection.clone()
}

fn reserve_port() -> Result<u16, Box<dyn std::error::Error>> {
    let listener = TcpListener::bind(("127.0.0.1", 0))?;
    Ok(listener.local_addr()?.port())
}

fn spawn_engine(port: u16, token: &str) -> Result<Child, Box<dyn std::error::Error>> {
    let arguments = [
        "--host".to_string(),
        "127.0.0.1".to_string(),
        "--port".to_string(),
        port.to_string(),
        "--token".to_string(),
        token.to_string(),
    ];

    if cfg!(debug_assertions) {
        let repository = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(|path| path.parent())
            .ok_or("unable to locate repository root")?;
        let source = repository.join("src");
        let python = std::env::var("LOGPILOT_PYTHON").unwrap_or_else(|_| "python".to_string());
        let mut child = Command::new(python)
            .arg("-m")
            .arg("logpilot.desktop_engine")
            .args(&arguments)
            .current_dir(repository)
            .env("PYTHONPATH", source)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()?;
        if let Err(error) = wait_for_engine(port, Duration::from_secs(15)) {
            let _ = child.kill();
            return Err(error);
        }
        return Ok(child);
    }

    let mut executable = std::env::current_exe()?;
    executable.set_file_name(if cfg!(target_os = "windows") {
        "logpilot-engine.exe"
    } else {
        "logpilot-engine"
    });
    if !executable.is_file() {
        return Err(format!("LogPilot Engine is missing: {}", executable.display()).into());
    }
    let mut command = Command::new(executable);
    command
        .args(arguments)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let mut child = command.spawn()?;
    if let Err(error) = wait_for_engine(port, Duration::from_secs(15)) {
        terminate_process_tree(&mut child);
        return Err(error);
    }
    Ok(child)
}

fn wait_for_engine(port: u16, timeout: Duration) -> Result<(), Box<dyn std::error::Error>> {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(80));
    }
    Err("LogPilot Engine did not become ready in time".into())
}

fn stop_engine(state: &DesktopState) {
    let connection = &state.connection;
    if let Ok(mut stream) = TcpStream::connect(connection.base_url.trim_start_matches("http://")) {
        let request = format!(
            "POST /api/shutdown HTTP/1.1\r\nHost: localhost\r\nX-LogPilot-Token: {}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            connection.token
        );
        let _ = stream.write_all(request.as_bytes());
        let _ = stream.flush();
    }
    if let Ok(mut child) = state.child.lock() {
        if let Some(mut process) = child.take() {
            let deadline = Instant::now() + Duration::from_secs(3);
            while Instant::now() < deadline {
                if matches!(process.try_wait(), Ok(Some(_))) {
                    return;
                }
                thread::sleep(Duration::from_millis(80));
            }
            terminate_process_tree(&mut process);
        }
    }
}

fn terminate_process_tree(process: &mut Child) {
    #[cfg(target_os = "windows")]
    {
        let _ = Command::new("taskkill")
            .args(["/PID", &process.id().to_string(), "/T", "/F"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
    let _ = process.kill();
    let _ = process.wait();
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![engine_connection])
        .setup(|app| {
            let port = reserve_port()?;
            let token = Uuid::new_v4().simple().to_string();
            let child = spawn_engine(port, &token)?;
            app.manage(DesktopState {
                connection: EngineConnection {
                    base_url: format!("http://127.0.0.1:{port}"),
                    token,
                },
                child: Mutex::new(Some(child)),
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build LogPilot desktop application");

    app.run(|handle, event| {
        if matches!(event, RunEvent::Exit | RunEvent::ExitRequested { .. }) {
            stop_engine(&handle.state::<DesktopState>());
        }
    });
}
