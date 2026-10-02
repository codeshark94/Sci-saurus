#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use std::os::unix::process::CommandExt;
use std::{
    fs,
    io::{BufRead, BufReader},
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

fn main() {
    let host = Host::default();
    let quit_host = host.clone();
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(host.clone())
        .invoke_handler(tauri::generate_handler![connect_saved, choose_repository])
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
                    if allowed
                        .as_ref()
                        .and_then(|s| tauri::Url::parse(s).ok())
                        .is_some_and(|local| local.origin() == url.origin())
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
        .run(move |_, event| {
            if matches!(event, tauri::RunEvent::Exit) {
                if let Ok(mut backend) = quit_host.0.lock() {
                    if let Some(mut child) = backend.child.take() {
                        stop_backend(&mut child);
                    }
                }
            }
        });
}
