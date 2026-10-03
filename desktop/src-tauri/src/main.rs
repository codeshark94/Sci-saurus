#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{mpsc, Arc, Mutex},
    time::Duration,
};
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;

fn stop_backend(child: &mut Child) {
    if matches!(child.try_wait(), Ok(Some(_))) {
        return;
    }
    let group = -(child.id() as i32);
    unsafe {
        libc::kill(group, libc::SIGTERM);
    }
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while std::time::Instant::now() < deadline {
        if matches!(child.try_wait(), Ok(Some(_))) {
            return;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    unsafe {
        libc::kill(group, libc::SIGKILL);
    }
    let _ = child.wait();
}

#[derive(Clone, Serialize, Deserialize)]
struct Workspace {
    repository: PathBuf,
    workspace: PathBuf,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Connection {
    url: String,
    socket_path: String,
    repository: String,
    workspace: String,
}

#[derive(Default)]
struct Backend {
    child: Option<Child>,
    connection: Option<Connection>,
}

#[derive(Clone, Default)]
struct Host(Arc<Mutex<Backend>>);

fn backend_request(
    host: &Host,
    request: tauri::http::Request<Vec<u8>>,
) -> Result<tauri::http::Response<Vec<u8>>, String> {
    if request.uri().host() != Some("localhost") {
        return Err("Unknown desktop resource origin".into());
    }
    let socket_path = host
        .0
        .lock()
        .map_err(|e| e.to_string())?
        .connection
        .as_ref()
        .ok_or("Backend is not connected")?
        .socket_path
        .clone();
    let mut stream = UnixStream::connect(socket_path).map_err(|e| e.to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(30)))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(30)))
        .map_err(|e| e.to_string())?;
    write!(
        stream,
        "{} {} HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\nContent-Length: {}\r\n",
        request.method(),
        request
            .uri()
            .path_and_query()
            .map(|p| p.as_str())
            .unwrap_or("/"),
        request.body().len()
    )
    .map_err(|e| e.to_string())?;
    for name in ["content-type", "origin"] {
        if let Some(value) = request.headers().get(name) {
            write!(
                stream,
                "{name}: {}\r\n",
                value.to_str().map_err(|e| e.to_string())?
            )
            .map_err(|e| e.to_string())?;
        }
    }
    stream.write_all(b"\r\n").map_err(|e| e.to_string())?;
    stream
        .write_all(request.body())
        .map_err(|e| e.to_string())?;
    let mut bytes = Vec::new();
    stream.read_to_end(&mut bytes).map_err(|e| e.to_string())?;
    let mut headers = vec![httparse::EMPTY_HEADER; 16];
    loop {
        let mut parsed = httparse::Response::new(&mut headers);
        match parsed.parse(&bytes) {
            Err(httparse::Error::TooManyHeaders) => {
                headers.resize(headers.len() * 2, httparse::EMPTY_HEADER);
            }
            Ok(httparse::Status::Complete(offset)) => {
                let mut response = tauri::http::Response::builder()
                    .status(parsed.code.ok_or("Missing backend response status")?);
                for header in parsed.headers {
                    response = response.header(header.name, header.value);
                }
                return response
                    .body(bytes[offset..].to_vec())
                    .map_err(|e| e.to_string());
            }
            result => return Err(format!("Invalid backend response: {result:?}")),
        }
    }
}

fn config_path(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_config_dir()
        .map(|p| p.join("workspace.json"))
        .map_err(|e| e.to_string())
}

fn launch(app: &tauri::AppHandle, host: &Host, workspace: Workspace) -> Result<Connection, String> {
    let mut backend = host.0.lock().map_err(|e| e.to_string())?;
    if let Some(child) = backend.child.as_mut() {
        if child.try_wait().map_err(|e| e.to_string())?.is_none() {
            let connected = backend
                .connection
                .as_ref()
                .ok_or("Backend has no connection")?;
            if connected.repository == workspace.repository.to_string_lossy()
                && connected.workspace == workspace.workspace.to_string_lossy()
            {
                return Ok(connected.clone());
            }
        } else {
            backend.child = None;
            backend.connection = None;
        }
    }
    if !workspace.repository.join("scisaurus/cli.py").is_file()
        || !workspace.repository.join(".venv/bin/python").is_file()
    {
        return Err("Choose a Sci-saurus repository with its .venv runtime installed.".into());
    }
    if !workspace.workspace.is_dir() || !workspace.workspace.starts_with(&workspace.repository) {
        return Err("The workspace must be inside the Sci-saurus repository.".into());
    }
    let config = config_path(app)?;
    fs::create_dir_all(config.parent().unwrap()).map_err(|e| e.to_string())?;
    let binary = if cfg!(debug_assertions) {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("binaries")
            .join(format!("scisaurus-backend-{}", env!("SCISAURUS_TARGET")))
    } else {
        std::env::current_exe()
            .map_err(|e| e.to_string())?
            .parent()
            .unwrap()
            .join("scisaurus-backend")
    };
    let log = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(config.with_file_name("backend.log"))
        .map_err(|e| e.to_string())?;
    let mut child = Command::new(binary)
        .args([
            "--repository",
            &workspace.repository.to_string_lossy(),
            "--workspace",
            &workspace.workspace.to_string_lossy(),
            "--parent-pipe",
        ])
        .current_dir(&workspace.repository)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(log)
        .process_group(0)
        .spawn()
        .map_err(|e| e.to_string())?;
    let stdout = child.stdout.take().ok_or("Backend stdout unavailable")?;
    let (sender, receiver) = mpsc::channel();
    std::thread::spawn(move || {
        let mut line = String::new();
        let mut reader = BufReader::new(stdout);
        let result = reader
            .read_line(&mut line)
            .map_err(|e| e.to_string())
            .and_then(|_| serde_json::from_str::<Connection>(&line).map_err(|e| e.to_string()));
        let _ = sender.send(result);
        for line in reader.lines() {
            if line.is_err() {
                break;
            }
        }
    });
    let connection = match receiver.recv_timeout(Duration::from_secs(30)) {
        Ok(Ok(connection)) => connection,
        error => {
            stop_backend(&mut child);
            return Err(format!(
                "Backend did not start: {error:?}. See {}",
                config.with_file_name("backend.log").display()
            ));
        }
    };
    if let Some(mut prior) = backend.child.take() {
        stop_backend(&mut prior);
    }
    backend.child = Some(child);
    backend.connection = Some(connection.clone());
    let temporary = config.with_extension("json.tmp");
    fs::write(
        &temporary,
        serde_json::to_vec_pretty(&workspace).map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())?;
    fs::rename(temporary, config).map_err(|e| e.to_string())?;
    Ok(connection)
}

#[tauri::command]
async fn connect_saved(
    app: tauri::AppHandle,
    host: tauri::State<'_, Host>,
) -> Result<Option<Connection>, String> {
    let host = host.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let args: Vec<String> = std::env::args().collect();
        let arg = |key: &str| {
            args.windows(2)
                .find(|pair| pair[0] == key)
                .map(|pair| PathBuf::from(&pair[1]))
        };
        let workspace = if let Some(repository) = arg("--repository") {
            let repository = repository.canonicalize().map_err(|e| e.to_string())?;
            let workspace = arg("--workspace")
                .unwrap_or_else(|| repository.join("local-private"))
                .canonicalize()
                .map_err(|e| e.to_string())?;
            Some(Workspace {
                repository,
                workspace,
            })
        } else {
            let config = config_path(&app)?;
            if config.is_file() {
                Some(
                    serde_json::from_slice::<Workspace>(
                        &fs::read(config).map_err(|e| e.to_string())?,
                    )
                    .map_err(|e| e.to_string())?,
                )
            } else {
                None
            }
        };
        workspace
            .map(|workspace| launch(&app, &host, workspace))
            .transpose()
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn choose_repository(
    app: tauri::AppHandle,
    host: tauri::State<'_, Host>,
) -> Result<Option<Connection>, String> {
    let host = host.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let folder = app
            .dialog()
            .file()
            .set_title("Choose the Sci-saurus repository")
            .blocking_pick_folder();
        let Some(folder) = folder else {
            return Ok(None);
        };
        let repository = folder
            .into_path()
            .map_err(|e| e.to_string())?
            .canonicalize()
            .map_err(|e| e.to_string())?;
        let workspace = if repository.join("local-private").is_dir() {
            repository.join("local-private")
        } else {
            repository.clone()
        };
        launch(
            &app,
            &host,
            Workspace {
                repository,
                workspace,
            },
        )
        .map(Some)
    })
    .await
    .map_err(|e| e.to_string())?
}

fn show_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        if let Err(error) = window.show().and_then(|_| window.set_focus()) {
            eprintln!("Unable to show the main window: {error}");
        }
    }
}

fn main() {
    let host = Host::default();
    let quit_host = host.clone();
    tauri::Builder::default()
        .register_asynchronous_uri_scheme_protocol("scisaurus", |context, request, responder| {
            let host = context.app_handle().state::<Host>().inner().clone();
            std::thread::spawn(move || {
                let response = backend_request(&host, request).unwrap_or_else(|error| {
                    tauri::http::Response::builder()
                        .status(502)
                        .header("Content-Type", "text/plain; charset=utf-8")
                        .body(error.into_bytes())
                        .unwrap()
                });
                responder.respond(response);
            });
        })
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            show_main_window(app);
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(host.clone())
        .invoke_handler(tauri::generate_handler![connect_saved, choose_repository])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                if let Err(error) = window.hide() {
                    eprintln!("Unable to hide the main window: {error}");
                }
            }
        })
        .setup(move |app| {
            let menu = tauri::menu::Menu::default(app.handle())?;
            let choose = tauri::menu::MenuItem::with_id(
                app,
                "choose-workspace",
                "Choose repository…",
                true,
                None::<&str>,
            )?;
            let reconnect = tauri::menu::MenuItem::with_id(
                app,
                "reconnect-backend",
                "Reconnect backend",
                true,
                None::<&str>,
            )?;
            menu.append(&tauri::menu::Submenu::with_items(
                app,
                "Workspace",
                true,
                &[&choose, &reconnect],
            )?)?;
            app.set_menu(menu)?;
            let navigation_host = host.clone();
            let handle = app.handle().clone();
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Sci-saurus")
                .inner_size(1440.0, 940.0)
                .min_inner_size(1100.0, 720.0)
                .on_navigation(move |url| {
                    if url.scheme() == "tauri" || url.host_str() == Some("tauri.localhost") {
                        return true;
                    }
                    let allowed = navigation_host
                        .0
                        .lock()
                        .ok()
                        .and_then(|backend| backend.connection.as_ref().map(|c| c.url.clone()));
                    if url.scheme() == "scisaurus"
                        && url.host_str() == Some("localhost")
                        && allowed.is_some()
                    {
                        return true;
                    }
                    if matches!(url.scheme(), "https" | "http") {
                        let _ = handle.opener().open_url(url.as_str(), None::<&str>);
                    }
                    false
                })
                .build()?;
            Ok(())
        })
        .on_menu_event(|app, event| {
            let app = app.clone();
            let id = event.id().as_ref().to_owned();
            if !matches!(id.as_str(), "choose-workspace" | "reconnect-backend") {
                return;
            }
            tauri::async_runtime::spawn(async move {
                let host = app.state::<Host>();
                let connection = if id == "choose-workspace" {
                    choose_repository(app.clone(), host).await
                } else {
                    connect_saved(app.clone(), host).await
                };
                match connection {
                    Ok(Some(connection)) => {
                        if let Some(window) = app.get_webview_window("main") {
                            if let Ok(url) = connection.url.parse() {
                                let _ = window.navigate(url);
                            }
                        }
                    }
                    Err(error) => {
                        app.dialog()
                            .message(error)
                            .title("Sci-saurus backend")
                            .show(|_| {});
                    }
                    _ => {}
                }
            });
        })
        .build(tauri::generate_context!())
        .expect("Desktop host initialization failed")
        .run(move |app, event| {
            #[cfg(target_os = "macos")]
            if matches!(event, tauri::RunEvent::Reopen { .. }) {
                show_main_window(app);
            }
            if matches!(event, tauri::RunEvent::Exit) {
                if let Ok(mut backend) = quit_host.0.lock() {
                    if let Some(mut child) = backend.child.take() {
                        stop_backend(&mut child);
                    }
                }
            }
        });
}
