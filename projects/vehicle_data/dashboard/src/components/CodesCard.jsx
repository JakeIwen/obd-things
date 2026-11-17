/**
 * Health view's Diagnostic codes card: the module filter, the code groups (rows open one at a time
 * into a panel), the Ignored group and the module coverage list. Reads one module-level `computed`
 * built from the `dtcs` and `dtc_ignores` summary slices and the in-memory module filter, so
 * nothing here re-renders per second. `slots` is the seam for more row actions (see CodeList).
 */

import { computed, signal } from "@preact/signals";
import * as store from "../store.js";
import { dayClock } from "../app/derive.js";
import { Card, Empty } from "./Card.jsx";
import LazyDisclosure from "./LazyDisclosure.jsx";
import MultiSelect from "./MultiSelect.jsx";
import CodeList from "./CodeList.jsx";
import DtcScan from "./DtcScan.jsx";
import * as H from "../views/health.helpers.js";

/** Chosen module keys for the session (in memory only); none means every module. */
export const moduleFilter = signal([]);

const scanEnabled = computed(() => store.web.value.dtc_jobs_enabled === true);
const codesVm = computed(() => {
  void dayClock.value;
  return H.codesView(store.summary.dtcs.value, Date.now(), scanEnabled.value, {
    ignores: store.summary.dtcIgnores.value,
    modules: moduleFilter.value,
  });
});

/** More row actions and a secondary line under a row's meaning (research controls plug in here). */
const SLOTS = {};

function ModuleList({ rows }) {
  return (
    <ul class="list">
      {rows.map((row) => (
        <li key={row.key} class="list__item">
          <span class={"list__mark" + (row.tone ? " list__mark--" + row.tone : "")} aria-hidden="true" />
          <div class="list__main">
            <div>{row.name}</div>
            <div class="list__meta">{row.meta}</div>
          </div>
        </li>
      ))}
    </ul>
  );
}

function CodeGroup({ group, canIgnore }) {
  if (group.total <= 0) {
    if (!group.moreText) return <div class="health-none">{group.title + " · none"}</div>;
    return (
      <div class="health-none">
        {group.title + " · none returned"}
        <p class="health-note">{group.moreText}</p>
      </div>
    );
  }
  return (
    <LazyDisclosure
      summary={group.summary}
      render={() => (
        <>
          {group.note ? <p class="health-note">{group.note}</p> : null}
          {group.rows.length ? <CodeList rows={group.rows} canIgnore={canIgnore} slots={SLOTS} /> : null}
          {group.moreText ? <p class="health-note">{group.moreText}</p> : null}
          {group.older.length ? (
            <LazyDisclosure
              cls="health-sub"
              summary={group.olderSummary}
              render={() => <CodeList rows={group.older} canIgnore={canIgnore} slots={SLOTS} />}
            />
          ) : null}
        </>
      )}
    />
  );
}

function IgnoredGroup({ group }) {
  if (group.total <= 0) return <div class="health-none">Ignored (0) · none in the chosen modules</div>;
  return (
    <LazyDisclosure
      cls="dtc-ignored"
      summary={group.summary}
      render={() => (
        <>
          <p class="health-note">Left out of every count above. They stay in the saved list; Restore brings one back.</p>
          <CodeList rows={group.rows} canIgnore slots={SLOTS} />
        </>
      )}
    />
  );
}

function Toolbar({ filter }) {
  return (
    <div class="dtc-toolbar">
      <MultiSelect id="dtc-modules" label="Modules" options={filter.options} selected={filter.chosen}
        onChange={(keys) => { moduleFilter.value = keys; }} />
    </div>
  );
}

export default function CodesCard() {
  const vm = codesVm.value;
  const web = store.web.value;
  const badge = vm.badge;
  const current = vm.current;
  const canIgnore = vm.ignoreAvailable;
  return (
    <Card
      title="Diagnostic codes"
      wide
      badge={badge ? badge.text : null}
      badgeTone={badge ? badge.tone : ""}
      foot={
        <a class="health-link" href={H.DOCS.caveats}>
          What “current” and “pending” mean
        </a>
      }
    >
      {vm.lastRead ? <p class="health-lead">{vm.lastRead}</p> : null}
      {vm.source ? <p class="status-line">{vm.source}</p> : null}
      {vm.ignoreNote ? <p class="health-note">{vm.ignoreNote}</p> : null}
      {vm.filter ? <Toolbar filter={vm.filter} /> : null}
      {vm.emptyText ? <Empty>{vm.emptyText}</Empty> : null}
      {current ? (
        <>
          <h3 class="section-title">{current.summary}</h3>
          {current.rows.length ? <CodeList rows={current.rows} canIgnore={canIgnore} slots={SLOTS} /> : <Empty>{current.moreText ? "None returned." : "No current codes."}</Empty>}
          {current.moreText ? <p class="health-note">{current.moreText}</p> : null}
        </>
      ) : null}
      {vm.groups.map((group) => (
        <CodeGroup key={group.key} group={group} canIgnore={canIgnore} />
      ))}
      {vm.ignored ? <IgnoredGroup key="ignored" group={vm.ignored} /> : null}
      {vm.modules && vm.modules.rows.length ? (
        <LazyDisclosure key="modules" summary={vm.modules.summary} render={() => <ModuleList rows={vm.modules.rows} />} />
      ) : null}
      {web.dtc_jobs_enabled === true ? <DtcScan key="scan" web={web} /> : null}
    </Card>
  );
}
