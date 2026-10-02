# Desktop app

Sci-saurus uses Tauri to host the research console and a bundled Python HTTP
backend on an ephemeral loopback port. The app is built for macOS desktop.

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

Closing the desktop app closes its UI backend. Research supervisors run in
separate sessions and continue until stopped explicitly or their workflow ends.
Reopening the app discovers those supervisors. A parent pipe also closes the
backend if the desktop host exits unexpectedly.

## Runtime boundary

The dashboard backend is bundled. Scientific workers run the selected
repository's `.venv/bin/python`, from that repository, using its current harness
and normal environment files. Research data, credentials, immutable artifacts,
and checkpoints remain in the repository; they are not copied into the app.

The backend binds only to `127.0.0.1`. Mutation requests require a local client,
the correct Host, same-origin browser requests, and JSON content. There is no
arbitrary command interface.
