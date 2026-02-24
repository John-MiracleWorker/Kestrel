import { S } from '../constants';
import { Toggle } from '../Shared';

interface PrReviewsTabProps {
    prEnabled: boolean;
    setPrEnabled: (v: boolean) => void;
    prAutoApprove: boolean;
    setPrAutoApprove: (v: boolean) => void;
    prPostComments: boolean;
    setPrPostComments: (v: boolean) => void;
    prSeverityFilter: string;
    setPrSeverityFilter: (v: string) => void;
}

export function PrReviewsTab({
    prEnabled,
    setPrEnabled,
    prAutoApprove,
    setPrAutoApprove,
    prPostComments,
    setPrPostComments,
    prSeverityFilter,
    setPrSeverityFilter
}: PrReviewsTabProps) {
    return (
        <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                <div style={S.sectionTitle}>// AUTONOMOUS CODE REVIEW</div>
                <Toggle value={prEnabled} onChange={setPrEnabled} />
            </div>

            <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '20px', lineHeight: 1.5 }}>
                Kestrel can automatically review PRs in your connected repositories via webhook.
            </p>

            {prEnabled && (
                <div style={{
                    padding: '16px', background: '#0d0d0d', border: '1px solid #1a1a1a',
                    borderRadius: '4px', marginBottom: '24px'
                }}>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', paddingBottom: '16px', borderBottom: '1px solid #1a1a1a' }}>
                            <div>
                                <div style={{ fontSize: '0.75rem', color: '#e0e0e0' }}>Post inline comments</div>
                                <div style={{ fontSize: '0.6rem', color: '#555' }}>Leave specific feedback on changed lines</div>
                            </div>
                            <Toggle value={prPostComments} onChange={setPrPostComments} />
                        </div>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', paddingBottom: '16px', borderBottom: '1px solid #1a1a1a' }}>
                            <div>
                                <div style={{ fontSize: '0.75rem', color: '#e0e0e0' }}>Auto-approve</div>
                                <div style={{ fontSize: '0.6rem', color: '#555' }}>Auto-approve clean PRs with no issues</div>
                            </div>
                            <Toggle value={prAutoApprove} onChange={setPrAutoApprove} />
                        </div>
                    </div>

                    <div style={{ ...S.field, marginTop: '16px', marginBottom: 0 }}>
                        <label style={S.label}>Severity filter</label>
                        <select style={S.input} value={prSeverityFilter} onChange={e => setPrSeverityFilter(e.target.value)}>
                            <option value="all">All issues</option>
                            <option value="high">High + Critical only</option>
                            <option value="critical">Critical only</option>
                        </select>
                    </div>
                </div>
            )}

            <div style={{ ...S.sectionTitle, marginTop: '24px' }}>// RECENT REVIEWS</div>
            <div style={{ textAlign: 'center', color: '#444', padding: '24px', fontSize: '0.8rem', fontStyle: 'italic' }}>
                Review history will appear here.
            </div>
        </div>
    );
}
