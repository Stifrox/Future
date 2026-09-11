// Receives vscode://future-local.future-self-update-bridge/update?prompt=... from Future's
// backend and drops the prompt straight into Copilot Chat, auto-submitted.
const vscode = require("vscode");

function activate(context) {
  context.subscriptions.push(
    vscode.window.registerUriHandler({
      async handleUri(uri) {
        const params = new URLSearchParams(uri.query);
        const prompt = params.get("prompt");
        if (!prompt) {
          vscode.window.showWarningMessage("Future self-update: no prompt was provided.");
          return;
        }
        try {
          await vscode.commands.executeCommand("workbench.action.chat.open", { query: prompt });
        } catch (err) {
          vscode.window.showErrorMessage(`Future self-update: could not open Copilot Chat (${err}).`);
        }
      },
    })
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
