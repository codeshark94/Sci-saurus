const status = document.getElementById("status"), detail = document.getElementById("detail");
async function connect(command) {
  for (const button of document.querySelectorAll("button")) button.disabled = true;
  status.textContent = "Connecting to your workspace…";
  try {
    const connection = await window.__TAURI__.core.invoke(command);
    if (connection) { window.location.replace(connection.url); return; }
    status.textContent = "Choose your research workspace";
    detail.textContent = "Select the Sci-saurus repository containing your runtime and missions.";
  } catch (error) { status.textContent = "Backend unavailable"; detail.textContent = String(error); }
  for (const button of document.querySelectorAll("button")) button.disabled = false;
}
document.getElementById("choose").addEventListener("click", () => connect("choose_repository"));
document.getElementById("retry").addEventListener("click", () => connect("connect_saved"));
connect("connect_saved");
