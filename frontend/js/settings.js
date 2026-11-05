// Settings dialog (GET/PATCH /config). Only the knobs a user would touch.
import { api } from "./api.js";
import { state } from "./state.js";
import { h, modal, toast, toastError } from "./ui.js";

export function openSettings() {
  const c = state.config || {};
  modal((close) => {
    const depth = h("input", { type: "number", class: "input", id: "set-depth", min: 1, max: 64, value: c.max_depth ?? 10 });
    const skip = h("textarea", { class: "input", id: "set-skip", rows: 3, spellcheck: "false" }, (c.skip_dir_names || []).join(", "));
    const perm = h("input", { type: "checkbox", id: "set-perm" });
    perm.checked = !!c.permanent_delete_default;
    const pageSize = h("input", { type: "number", class: "input", id: "set-page", min: 20, max: 1000, step: 10, value: c.page_size ?? 100 });
    const err = h("p", { class: "form-err", hidden: true });

    const save = async () => {
      const patch = {
        max_depth: parseInt(depth.value, 10),
        skip_dir_names: skip.value.split(/[,\n]/).map((s) => s.trim()).filter(Boolean),
        permanent_delete_default: perm.checked,
        page_size: parseInt(pageSize.value, 10),
      };
      try {
        state.config = await api.patchConfig(patch);
        toast("Settings saved.");
        close(true);
      } catch (e) {
        err.hidden = false;
        err.textContent = e.message;
        if (e.status !== 422) toastError(e, "Couldn't save settings");
      }
    };

    return h("div", { class: "modal-inner settings" },
      h("h2", { class: "modal-title" }, "Settings"),
      h("div", { class: "field" },
        h("label", { for: "set-depth" }, "Itemise folders down to this depth"),
        depth,
        h("p", { class: "field-help" }, "Deeper folders are still measured in full, just shown as one block. Applies to new scans.")),
      h("div", { class: "field" },
        h("label", { for: "set-skip" }, "Always measure these folders as one block"),
        skip,
        h("p", { class: "field-help" }, "Comma-separated folder names, such as node_modules or .git. Applies to new scans.")),
      h("div", { class: "field" },
        h("label", { for: "set-page" }, "Rows to load at a time in the list"),
        pageSize),
      h("label", { class: "check-row" }, perm, h("span", {}, "Pre-select permanent deletion when removing (you still have to type delete to confirm)")),
      err,
      h("div", { class: "modal-actions" },
        h("button", { type: "button", class: "btn btn-ghost", onclick: () => close() }, "Cancel"),
        h("button", { type: "button", class: "btn btn-primary", onclick: save }, "Save")),
    );
  }, { label: "Settings" });
}
