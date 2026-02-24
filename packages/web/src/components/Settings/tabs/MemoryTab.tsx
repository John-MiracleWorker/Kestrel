import { S } from '../constants';
import { Toggle, SliderField } from '../Shared';

interface MemoryTabProps {
    ragEnabled: boolean;
    setRagEnabled: (enabled: boolean) => void;
    ragTopK: number;
    setRagTopK: (topK: number) => void;
    ragMinSimilarity: number;
    setRagMinSimilarity: (sim: number) => void;
}

export function MemoryTab({
    ragEnabled,
    setRagEnabled,
    ragTopK,
    setRagTopK,
    ragMinSimilarity,
    setRagMinSimilarity
}: MemoryTabProps) {
    return (
        <div>
            <div style={S.sectionTitle}>// RAG RETRIEVAL</div>
            <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '20px', lineHeight: 1.5 }}>
                Control how Kestrel retrieves context from conversation memory and workspace knowledge.
            </p>
            <div style={{ ...S.field, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <div>
                    <label style={{ ...S.label, marginBottom: 0 }}>Enable Memory Retrieval</label>
                    <div style={{ fontSize: '0.7rem', color: '#444', marginTop: '4px' }}>Augment responses with relevant past context</div>
                </div>
                <Toggle value={ragEnabled} onChange={setRagEnabled} />
            </div>
            {ragEnabled && (
                <div style={{ borderTop: '1px solid #1a1a1a', paddingTop: '20px', marginTop: '4px' }}>
                    <SliderField label="Retrieval Count (top-k)" value={ragTopK}
                        onChange={v => setRagTopK(Math.round(v))} min={1} max={20} step={1}
                        format={v => `${v} chunks`} />
                    <SliderField label="Similarity Threshold" value={ragMinSimilarity}
                        onChange={setRagMinSimilarity} min={0} max={1} step={0.05}
                        format={v => `${(v * 100).toFixed(0)}%`} />
                    <div style={{ padding: '12px', background: '#0d0d0d', borderRadius: '4px', border: '1px solid #1a1a1a' }}>
                        <div style={{ fontSize: '0.7rem', color: '#666', lineHeight: 1.6 }}>
                            <span style={{ color: '#00f3ff' }}>top-k = {ragTopK}</span> — chunks retrieved per query<br />
                            <span style={{ color: '#00f3ff' }}>threshold = {(ragMinSimilarity * 100).toFixed(0)}%</span> — minimum similarity
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
