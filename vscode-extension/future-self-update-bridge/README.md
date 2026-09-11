# Future Self-Update Bridge

Internal VS Code extension used by the Future desktop assistant. It registers a
`vscode://future-local.future-self-update-bridge/update?prompt=...` URI handler that opens
Copilot Chat with the given prompt pre-filled and auto-submitted, so self-update requests
from Future run without any manual copy/paste.

Not published to the Marketplace - installed locally via the `.vsix` in this folder.
