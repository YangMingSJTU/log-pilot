use std::{
    fs::{self, OpenOptions},
    io::Write,
    net::{TcpListener, TcpStream},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::Mutex,
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
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

const LOG_MAX_BYTES: u64 = 5 * 1024 * 1024;
const LOG_BACKUP_COUNT: usize = 3;

fn app_data_root() -> Option<PathBuf> {
    if let Some(path) = std::env::var_os("LOGPILOT_DATA_DIR").filter(|value| !value.is_empty()) {
        return Some(PathBuf::from(path));
    }
    if cfg!(target_os = "windows") {
        return std::env::var_os("LOCALAPPDATA")
            .or_else(|| std::env::var_os("APPDATA"))
            .map(PathBuf::from)
            .map(|path| path.join("LogPilot"));
    }
    if cfg!(target_os = "macos") {
        return std::env::var_os("HOME").map(PathBuf::from).map(|path| {
            path.join("Library")
                .join("Application Support")
                .join("LogPilot")
        });
    }
    std::env::var_os("XDG_DATA_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOME")
                .map(PathBuf::from)
                .map(|path| path.join(".local").join("share"))
        })
        .map(|path| path.join("logpilot"))
}

fn desktop_log_path() -> Option<PathBuf> {
    app_data_root().map(|path| path.join("logs").join("logpilot-desktop.log"))
}

fn rotate_desktop_log(path: &PathBuf) {
    let Ok(metadata) = fs::metadata(path) else {
        return;
    };
    if metadata.len() < LOG_MAX_BYTES {
        return;
    }
    for index in (1..=LOG_BACKUP_COUNT).rev() {
        let source = if index == 1 {
            path.clone()
        } else {
            PathBuf::from(format!("{}.{}", path.display(), index - 1))
        };
        let destination = PathBuf::from(format!("{}.{}", path.display(), index));
        if index == LOG_BACKUP_COUNT {
            let _ = fs::remove_file(&destination);
        }
        if source.exists() {
            let _ = fs::rename(source, destination);
        }
    }
}

fn write_desktop_log(level: &str, event: &str) {
    let Some(path) = desktop_log_path() else {
        return;
    };
    let Some(parent) = path.parent() else {
        return;
    };
    if fs::create_dir_all(parent).is_err() {
        return;
    }
    rotate_desktop_log(&path);
    let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) else {
        return;
    };
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_millis())
        .unwrap_or(0);
    let _ = writeln!(
        file,
        "{timestamp} {level} pid={} {event}",
        std::process::id()
    );
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
    write_desktop_log(
        "INFO",
        &format!(
            "engine_spawn_requested port={port} mode={}",
            if cfg!(debug_assertions) {
                "development"
            } else {
                "sidecar"
            }
        ),
    );
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
            write_desktop_log(
                "ERROR",
                &format!("engine_start_timeout port={port} error={error}"),
            );
            let _ = child.kill();
            return Err(error);
        }
        write_desktop_log(
            "INFO",
            &format!("engine_ready port={port} pid={}", child.id()),
        );
        return Ok(child);
    }

    let mut executable = std::env::current_exe()?;
    executable.set_file_name(if cfg!(target_os = "windows") {
        "logpilot-engine.exe"
    } else {
        "logpilot-engine"
    });
    if !executable.is_file() {
        write_desktop_log(
            "ERROR",
            &format!("engine_sidecar_missing path={}", executable.display()),
        );
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
        write_desktop_log(
            "ERROR",
            &format!("engine_start_timeout port={port} error={error}"),
        );
        terminate_process_tree(&mut child);
        return Err(error);
    }
    write_desktop_log(
        "INFO",
        &format!("engine_ready port={port} pid={}", child.id()),
    );
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
    write_desktop_log("INFO", "engine_stop_requested");
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
                    write_desktop_log("INFO", "engine_stopped_gracefully");
                    return;
                }
                thread::sleep(Duration::from_millis(80));
            }
            terminate_process_tree(&mut process);
            write_desktop_log("WARNING", "engine_force_stopped");
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
    write_desktop_log("INFO", "desktop_starting");
    let app_result = tauri::Builder::default()
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
            let child = match spawn_engine(port, &token) {
                Ok(child) => child,
                Err(error) => {
                    write_desktop_log("ERROR", &format!("engine_start_failed error={error}"));
                    return Err(error);
                }
            };
            app.manage(DesktopState {
                connection: EngineConnection {
                    base_url: format!("http://127.0.0.1:{port}"),
                    token,
                },
                child: Mutex::new(Some(child)),
            });
            Ok(())
        })
        .build(tauri::generate_context!());
    let app = match app_result {
        Ok(app) => app,
        Err(error) => {
            write_desktop_log("ERROR", &format!("desktop_build_failed error={error}"));
            panic!("failed to build LogPilot desktop application: {error}");
        }
    };
    write_desktop_log("INFO", "desktop_started");

    app.run(|handle, event| {
        if matches!(event, RunEvent::Exit | RunEvent::ExitRequested { .. }) {
            stop_engine(&handle.state::<DesktopState>());
            write_desktop_log("INFO", "desktop_stopped");
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn desktop_log_uses_the_configured_user_data_directory() {
        let previous = std::env::var_os("LOGPILOT_DATA_DIR");
        let data_dir =
            std::env::temp_dir().join(format!("logpilot-desktop-log-{}", Uuid::new_v4()));
        std::env::set_var("LOGPILOT_DATA_DIR", &data_dir);

        write_desktop_log("INFO", "desktop_log_test");

        let path = data_dir.join("logs").join("logpilot-desktop.log");
        let content = fs::read_to_string(&path).expect("desktop log should be readable");
        assert!(content.contains("desktop_log_test"));
        if let Some(value) = previous {
            std::env::set_var("LOGPILOT_DATA_DIR", value);
        } else {
            std::env::remove_var("LOGPILOT_DATA_DIR");
        }
        fs::remove_dir_all(data_dir).expect("temporary log directory should be removable");
    }
}
