import { FileSearch } from "lucide-react";
import {
  Disclosure,
  JsonBlock,
} from "../components";

export type EvidenceRecord = {
  label: string;
  value: unknown;
};

export function EvidenceDrawer({
  title,
  records,
}: {
  title: string;
  records: EvidenceRecord[];
}) {
  if (records.length === 0) return null;

  return (
    <section
      className="mission-evidence-drawer"
      aria-label={title}
    >
      <header>
        <FileSearch size={17} aria-hidden="true" />
        <div>
          <h3>{title}</h3>
          <p>
            Raw records stay collapsed until you choose to inspect
            them.
          </p>
        </div>
      </header>
      <div className="mission-evidence-records">
        {records.map((record) => (
          <Disclosure
            key={record.label}
            title={`${record.label} evidence`}
          >
            <JsonBlock value={record.value} maxHeight="240px" />
          </Disclosure>
        ))}
      </div>
    </section>
  );
}
