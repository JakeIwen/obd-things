/**
 * What the code research adds to a Codes-card row (CodeList's `slots`): the `AI:` line under the
 * meaning and the panel button that opens the research dialog. The dialog itself is a lazy chunk;
 * only these few lines and the list signals load with the Health view.
 */

import { openDialog } from "../app/dialogs.js";
import { researchItems, researchStatus } from "../dtcResearch.js";
import { AI_LINE_TITLE, itemKey, researchButtonText, researchable, rowExtraLine } from "../dtcResearch.helpers.js";

const itemOf = (row) => researchItems.value.get(itemKey(row.moduleKey, row.fca)) || null;

export const RESEARCH_SLOTS = {
  extra(row) {
    if (researchStatus.value === "missing") return null;
    const line = rowExtraLine(itemOf(row));
    return line ? (
      <>
        {line}{" "}
        <span class="dtc-ai-info" title={AI_LINE_TITLE} role="img" aria-label={AI_LINE_TITLE}>i</span>
      </>
    ) : null;
  },
  actions(row) {
    if (researchStatus.value === "missing" || !researchable(row.moduleKey, row.fca)) return null;
    const target = { module_key: row.moduleKey, fca_display: row.fca, code: row.code, meaning: row.meaning, moduleName: row.moduleName };
    return (
      <button type="button" class="btn btn--small" onClick={() => openDialog("research", { target })}>
        {researchButtonText(itemOf(row))}
      </button>
    );
  },
};
