"use strict";

const { dialog, shell } = require("electron");
const path = require("node:path");

const FILE_ACCESS_SCOPES = {
  WORKSPACE: "workspace",
  HOME: "home",
  SYSTEM: "system",
  EXTERNAL: "external",
};

const FILE_OPERATIONS = ["read", "write", "delete", "list"];

/**
 * Determine the scope of a file path relative to the workspace and home directories.
 */
function getFileScope(filePath, workspaceRoot, homeDir) {
  const resolvedPath = path.resolve(filePath);
  const workspacePath = path.resolve(workspaceRoot);

  // Check if it's inside the workspace
  if (resolvedPath.startsWith(workspacePath + path.sep) || resolvedPath === workspacePath) {
    return FILE_ACCESS_SCOPES.WORKSPACE;
  }

  // Check if it's in the user's home directory
  if (homeDir && resolvedPath.startsWith(homeDir + path.sep)) {
    return FILE_ACCESS_SCOPES.HOME;
  }

  // Check if it's a system path
  const systemPaths = [
    "/bin", "/sbin", "/usr/bin", "/usr/sbin", "/etc", "/lib", "/lib64",
    "/usr/lib", "/usr/lib64", "/var", "/etc", "/boot", "/sys", "/proc",
    "C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)",
    "C:\\System32", "C:\\SysWOW64",
  ];

  for (const sysPath of systemPaths) {
    if (resolvedPath.startsWith(path.resolve(sysPath) + path.sep)) {
      return FILE_ACCESS_SCOPES.SYSTEM;
    }
  }

  return FILE_ACCESS_SCOPES.EXTERNAL;
}

/**
 * Check if the file operation is allowed based on scope and operation.
 */
function isOperationAllowed(scope, operation) {
  if (!FILE_OPERATIONS.includes(operation)) return false;

  switch (scope) {
    case FILE_ACCESS_SCOPES.WORKSPACE:
      return true; // Always allow workspace operations
    case FILE_ACCESS_SCOPES.HOME:
      return FILE_OPERATIONS.includes(operation); // Allow all operations in home
    case FILE_ACCESS_SCOPES.SYSTEM:
      return false; // Never allow system operations by default
    case FILE_ACCESS_SCOPES.EXTERNAL:
      return false; // External requires explicit permission
    default:
      return false;
  }
}

/**
 * Show a file access permission dialog to the user.
 */
async function showFileAccessDialog(window, options) {
  const {
    path: filePath,
    operation,
    scope,
    source = "backend",
    details = "",
  } = options;

  const scopeLabels = {
    workspace: "Workspace",
    home: "Home Directory",
    system: "System Files",
    external: "External Location",
  };

  const operationLabels = {
    read: "read",
    write: "write to",
    delete: "delete",
    list: "list files in",
  };

  const scopeLabel = scopeLabels[scope] || scope;
  const operationLabel = operationLabels[operation] || operation;

  const message = `The ${source} is requesting permission to ${operationLabel} a file in ${scopeLabel.toLowerCase()}.`;
  const detail = [
    `Path: ${filePath}`,
    `Operation: ${operationLabel}`,
    `Scope: ${scopeLabel}`,
    details ? `Details: ${details}` : "",
  ].filter(Boolean).join("\n");

  const buttons = [
    "Deny",
    "Allow Once",
    "Always Allow",
    "Always Deny",
  ];

  const response = await dialog.showMessageBox(window, {
    type: "question",
    title: "File Access Request",
    message,
    detail,
    buttons,
    defaultId: 1, // "Allow Once" as default
    cancelId: 0, // "Deny" as cancel
    noLink: true,
    icon: undefined, // Could add a custom icon
  });

  const responseMap = {
    0: "deny",
    1: "allow",
    2: "always_allow",
    3: "always_deny",
  };

  return {
    decision: responseMap[response.response] || "deny",
    checkboxChecked: false, // We don't use checkboxes in this dialog
  };
}

/**
 * Register IPC handlers for file access permissions.
 */
function registerFileAccessIpcHandlers({ ipcMain, getWindow, getWorkspaceRoot, getHomeDir }) {
  ipcMain.handle("file-access:request", async (event, options) => {
    if (!isAverQelSender(event)) {
      throw new Error("Unauthorized file access request.");
    }

    const window = getWindow();
    if (!window) {
      return { decision: "deny", reason: "No window available" };
    }

    try {
      const result = await showFileAccessDialog(BrowserWindow.fromWebContents(event.sender), options);
      return { success: true, ...result };
    } catch (error) {
      console.error("File access dialog error:", error);
      return { success: false, decision: "deny", reason: error.message };
    }
  });

  ipcMain.handle("file-access:check", async (event, { path, operation }) => {
    if (!isAverQelSender(event)) {
      throw new Error("Unauthorized file access check.");
    }

    const workspaceRoot = getWorkspaceRoot();
    const homeDir = getHomeDir();
    const scope = getFileScope(path, workspaceRoot, process.env.HOME || process.env.USERPROFILE);
    const allowed = isOperationAllowed(scope, operation);

    return {
      success: true,
      allowed,
      scope,
      path,
      operation,
    };
  });

  ipcMain.handle("file-access:list-permissions", async (event) => {
    if (!isAverQelSender(event)) {
      throw new Error("Unauthorized file access list request.");
    }

    // Return stored permissions from the backend service
    // This would be implemented to return stored permissions
    return { success: true, permissions: [] };
  });

  ipcMain.handle("file-access:grant", async (event, { path, scope, operations, decision }) => {
    if (!isAverQelSender(event)) {
      throw new Error("Unauthorized permission grant.");
    }

    // Forward to backend to persist
    return { success: true };
  });

  ipcMain.handle("file-access:revoke", async (event, { path, scope }) => {
    if (!isAverQelSender(event)) {
      throw new Error("Unauthorized permission revoke.");
    }

    return { success: true };
  });
}

function isAverQelSender(event) {
  // Simple check - in production this would be more robust
  return event.sender && event.sender.getURL().startsWith("http://127.0.0.1:11030");
}

module.exports = {
  FILE_ACCESS_SCOPES,
  FILE_OPERATIONS,
  getFileScope,
  isOperationAllowed,
  showFileAccessDialog,
  registerFileAccessIpcHandlers,
  isAverQelSender,
};