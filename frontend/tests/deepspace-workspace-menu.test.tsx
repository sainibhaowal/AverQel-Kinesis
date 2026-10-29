import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import DeepSpaceWorkspaceMenu from "../app/dashboard/deepspace/_components/DeepSpaceWorkspaceMenu";
import type { Workspace } from "../app/dashboard/deepspace/_lib/workspace-api";

const browseDirectoryMock = vi.hoisted(() => vi.fn());

vi.mock("../app/dashboard/deepspace/_lib/workspace-api", () => ({
  browseDirectory: (...args: unknown[]) => browseDirectoryMock(...args),
  createDirectory: vi.fn(),
}));

const WORKSPACES: Workspace[] = [
  {
    id: "default",
    name: "default-workspace",
    path: "/home/user/workspaces/default",
    is_default: true,
  },
  {
    id: "billing-service",
    name: "billing-service",
    path: "/home/user/workspaces/billing-service",
    is_default: false,
  },
];

/**
 * The workspace menu lists the workspaces the user holds, marks the one the
 * conversation works in, and can add another.
 */
describe("DeepSpaceWorkspaceMenu", () => {
  beforeEach(() => {
    browseDirectoryMock.mockReset();
    browseDirectoryMock.mockResolvedValue({
      path: "/home/user",
      home: "/home/user",
      crumbs: [{ name: "Home", path: "/home/user", hidden: false }],
      entries: [],
      truncated: false,
    });
  });

  it("shows the workspace in force and lists the others", () => {
    render(
      <DeepSpaceWorkspaceMenu
        workspaces={WORKSPACES}
        workspace={WORKSPACES[0]}
        onWorkspaceChange={vi.fn()}
        onCreateWorkspace={vi.fn()}
      />,
    );

    const trigger = screen.getByRole("button", { name: "Workspace" });
    expect(trigger).toHaveTextContent("default-workspace");

    fireEvent.click(trigger);

    expect(screen.getByRole("menuitemradio", { name: /default-workspace/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("menuitemradio", { name: /billing-service/ })).toHaveAttribute(
      "aria-checked",
      "false",
    );
  });

  it("reports the workspace the user picks", () => {
    const onWorkspaceChange = vi.fn();
    render(
      <DeepSpaceWorkspaceMenu
        workspaces={WORKSPACES}
        workspace={WORKSPACES[0]}
        onWorkspaceChange={onWorkspaceChange}
        onCreateWorkspace={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Workspace" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: /billing-service/ }));

    expect(onWorkspaceChange).toHaveBeenCalledWith("billing-service");
  });

  it("opens the Select Workspace Directory modal on Add workspace click", () => {
    const onCreateWorkspace = vi.fn();
    render(
      <DeepSpaceWorkspaceMenu
        workspaces={WORKSPACES}
        workspace={WORKSPACES[0]}
        onWorkspaceChange={vi.fn()}
        onCreateWorkspace={onCreateWorkspace}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Workspace" }));
    fireEvent.click(screen.getByRole("menuitem", { name: /Add workspace/ }));

    expect(screen.getByRole("dialog", { name: "Select Workspace Directory" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New folder" })).toBeInTheDocument();
  });

  it("passes the selected host directory to workspace creation", async () => {
    browseDirectoryMock.mockResolvedValue({
      path: "/home/user",
      home: "/home/user",
      crumbs: [{ name: "Home", path: "/home/user", hidden: false }],
      entries: [{ name: "chosen-project", path: "/srv/projects/chosen-project", hidden: false }],
      truncated: false,
    });
    const onCreateWorkspace = vi.fn();
    render(
      <DeepSpaceWorkspaceMenu
        workspaces={WORKSPACES}
        workspace={WORKSPACES[0]}
        onWorkspaceChange={vi.fn()}
        onCreateWorkspace={onCreateWorkspace}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Workspace" }));
    fireEvent.click(screen.getByRole("menuitem", { name: /Add workspace/ }));
    fireEvent.click(await screen.findByRole("button", { name: "chosen-project" }));
    fireEvent.click(screen.getByRole("button", { name: "Open" }));

    expect(onCreateWorkspace).toHaveBeenCalledWith(
      "chosen-project",
      "/srv/projects/chosen-project",
    );
  });

  it("closes the Select Workspace Directory modal on Cancel", () => {
    const onCreateWorkspace = vi.fn();
    render(
      <DeepSpaceWorkspaceMenu
        workspaces={WORKSPACES}
        workspace={WORKSPACES[0]}
        onWorkspaceChange={vi.fn()}
        onCreateWorkspace={onCreateWorkspace}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Workspace" }));
    fireEvent.click(screen.getByRole("menuitem", { name: /Add workspace/ }));

    expect(screen.getByRole("dialog", { name: "Select Workspace Directory" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog", { name: "Select Workspace Directory" })).not.toBeInTheDocument();
    expect(onCreateWorkspace).not.toHaveBeenCalled();
  });

  it("calls onDeleteWorkspace when non-default workspace remove button is clicked", () => {
    const onDeleteWorkspace = vi.fn();
    render(
      <DeepSpaceWorkspaceMenu
        workspaces={WORKSPACES}
        workspace={WORKSPACES[0]}
        onWorkspaceChange={vi.fn()}
        onCreateWorkspace={vi.fn()}
        onDeleteWorkspace={onDeleteWorkspace}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Workspace" }));
    const deleteBtn = screen.getByRole("button", { name: "Remove workspace billing-service" });
    fireEvent.click(deleteBtn);

    expect(onDeleteWorkspace).toHaveBeenCalledWith("billing-service");
  });
});
