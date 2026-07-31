import { Search } from "lucide-react";
import type { FormEvent } from "react";
import type { MemoryHit } from "../types";
import {
  Disclosure,
  EmptyState,
  Field,
  InlineMeta,
  JsonBlock,
  Panel,
} from "../components";
import { POLICY_AUTHORITY_LABEL } from "./MemoryHealth";

export function MemorySearch({
  memoryQuery,
  memoryHits,
  memoryInspect,
  onQueryChange,
  onSearch,
}: {
  memoryQuery: string;
  memoryHits: MemoryHit[];
  memoryInspect: Record<string, unknown> | null;
  onQueryChange: (value: string) => void;
  onSearch: (event?: FormEvent) => void;
}) {
  return (
    <Panel title="Memory search" icon={<Search size={19} />}>
      <section aria-label="Memory search" className="memory-search">
        <form onSubmit={onSearch} className="inline-form">
          <Field label="Memory query">
            <input
              value={memoryQuery}
              onChange={(event) => onQueryChange(event.target.value)}
            />
          </Field>
          <button type="submit">
            <Search size={15} aria-hidden="true" /> Search
          </button>
        </form>
        <div className="hit-list">
          {memoryHits.map((hit) => (
            <div
              className="data-row"
              key={`${hit.layer}-${hit.record_id ?? hit.title}`}
            >
              <strong>{hit.title}</strong>
              <InlineMeta
                items={[hit.layer, hit.kind, hit.score.toFixed(2)]}
              />
              <p>{hit.snippet}</p>
              {hit.layer === "policy" ? (
                <p className="memory-policy-gate">
                  {POLICY_AUTHORITY_LABEL}
                </p>
              ) : null}
            </div>
          ))}
          {memoryHits.length === 0 ? (
            <EmptyState>
              Run a search to see ranked memory evidence with provenance.
            </EmptyState>
          ) : null}
        </div>
        {memoryInspect ? (
          <Disclosure title="Search inspection evidence">
            <JsonBlock value={memoryInspect} maxHeight="260px" />
          </Disclosure>
        ) : null}
      </section>
    </Panel>
  );
}
