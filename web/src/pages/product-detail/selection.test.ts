import { describe, expect, it } from "vitest";

import {
  clearSelectedNodeGroup,
  deleteNodeFromSelection,
  focusSelectedNodeGroup,
  reconcileSelectedNodeIds,
  replaceSelectedNodeIdsFromBox,
  toggleSelectedNodeId,
} from "./selection";

describe("workflow canvas selection helpers", () => {
  it("toggles ids while preserving selection order", () => {
    expect(toggleSelectedNodeId(["a"], "b")).toEqual(["a", "b"]);
    expect(toggleSelectedNodeId(["a", "b", "c"], "b")).toEqual(["a", "c"]);
  });

  it("focuses a selected group member without collapsing the group", () => {
    expect(focusSelectedNodeGroup(["a", "b", "c"], "b")).toEqual({
      selectedNodeIds: ["a", "b", "c"],
      primaryNodeId: "b",
    });
    expect(focusSelectedNodeGroup(["a"], "b")).toEqual({
      selectedNodeIds: ["b"],
      primaryNodeId: "b",
    });
  });

  it("clears a selected group back to the primary node", () => {
    expect(clearSelectedNodeGroup("b")).toEqual(["b"]);
    expect(clearSelectedNodeGroup(null)).toEqual([]);
  });

  it("deleting a node exits multi-select and keeps a single primary node", () => {
    expect(deleteNodeFromSelection(["a", "b", "c"], "b", "b")).toEqual({
      selectedNodeIds: ["a"],
      primaryNodeId: "a",
    });
    expect(deleteNodeFromSelection(["a", "b", "c"], "c", "a")).toEqual({
      selectedNodeIds: ["a"],
      primaryNodeId: "a",
    });
    expect(deleteNodeFromSelection(["a"], "a", "a")).toEqual({
      selectedNodeIds: [],
      primaryNodeId: null,
    });
  });

  it("replaces selection from lasso results and falls back to the primary node for empty boxes", () => {
    expect(replaceSelectedNodeIdsFromBox(["b", "c"], "a")).toEqual({
      selectedNodeIds: ["b", "c"],
      primaryNodeId: "b",
    });
    expect(replaceSelectedNodeIdsFromBox([], "a")).toEqual({
      selectedNodeIds: ["a"],
      primaryNodeId: "a",
    });
    expect(replaceSelectedNodeIdsFromBox([], null)).toEqual({
      selectedNodeIds: [],
      primaryNodeId: null,
    });
  });

  it("reconciles deleted nodes and keeps the primary node selected", () => {
    expect(reconcileSelectedNodeIds(["a", "missing", "c"], [{ id: "a" }, { id: "b" }, { id: "c" }], "c")).toEqual({
      selectedNodeIds: ["a", "c"],
      primaryNodeId: "c",
    });
    expect(reconcileSelectedNodeIds(["missing"], [{ id: "a" }, { id: "b" }], "missing")).toEqual({
      selectedNodeIds: ["a"],
      primaryNodeId: "a",
    });
    expect(reconcileSelectedNodeIds(["b"], [{ id: "a" }, { id: "b" }], "a")).toEqual({
      selectedNodeIds: ["a", "b"],
      primaryNodeId: "a",
    });
  });
});
