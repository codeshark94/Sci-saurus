# Desktop app

Sci-saurus uses Tauri to host the research console and a bundled Python backend.
The app is built for macOS desktop. Its `scisaurus://` resources are served through
an app protocol and a private Unix socket; the app does not open a TCP web port.

## Build and open

Install Node.js, Rust, and the Xcode command line tools. Initialize the research
repository's `.venv` using the normal project setup. From the repository root:

```bash
cd desktop
npm ci
npm run build
cd ..
./desktop-app
```

The build creates `desktop/src-tauri/target/release/bundle/macos/Sci-saurus.app`.
It can be copied to Applications. The build always regenerates its PyInstaller
backend before packaging; Python build dependencies are isolated in
`desktop/.venv-build`. `npm run dev` runs the desktop development window.

The app saves the selected repository and research workspace in its application
support directory. `Workspace → Choose repository…` changes the selection;
`Reconnect backend` reconnects to the current workspace. Opening the app does
not launch a mission.

## Execution controls

Select a project to expose Start, Stop, Resume, and its scheduled stage boundary.
The backend reads the durable checkpoint and verifies the actual supervisor
process before enabling actions. Stop ends the supervised worker tree and
preserves its checkpoint. Resume retains acquired evidence and the original
mission deadline. Existing state is never overwritten by Start.

The window close button hides the window while keeping the app and its backend
running. Selecting the Dock icon or opening the app again restores the window.
Quit (Command-Q) exits the app and closes its UI backend. Research supervisors run in
separate sessions and continue until stopped explicitly or their workflow ends.
Reopening the app discovers those supervisors. A parent pipe also closes the
backend if the desktop host exits unexpectedly.

## Runtime boundary

The dashboard backend is bundled. Scientific workers run the selected
repository's `.venv/bin/python`, from that repository, using its current harness
and normal environment files. Research data, credentials, immutable artifacts,
and checkpoints remain in the repository; they are not copied into the app.

The backend socket and its temporary directory are accessible only to the current
user. Tauri forwards resource and API requests over this socket. Mutation requests
require JSON content; any supplied Origin must match the app origin. There is no
arbitrary command interface.
The separate `./dashboard` command remains an explicit browser development tool;
the desktop app does not launch it.
