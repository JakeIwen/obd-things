import { LightIndicators } from "../components/LightIndicators.jsx";
import { Compass } from "../components/Compass.jsx";
import { DRIVE_PAGE_NAMES } from "./drivePager.js";

/** Shared by the lazy Drive pages so the icons stay out of the boot bundle. */
export function DriveFooter({ page, onPageChange }) {
  return (
    <div class="drive-footer">
      <LightIndicators />
      <nav class="drive-pager" aria-label="Drive pages">
        {DRIVE_PAGE_NAMES.map(([n, name]) => (
          <button
            key={n}
            type="button"
            class={"drive-pager__btn" + (page === n ? " drive-pager__btn--on" : "")}
            aria-current={page === n ? "page" : undefined}
            onClick={() => onPageChange(n)}
          >
            <span class="drive-pager__num">{n}</span>
            {name}
          </button>
        ))}
      </nav>
      <Compass />
    </div>
  );
}
