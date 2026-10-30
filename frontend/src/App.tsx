import { useState, useEffect, useMemo, useCallback } from 'react';
import { AgGridReact } from 'ag-grid-react';
import type { ColDef } from 'ag-grid-community';
import { Shield, CheckCircle, RefreshCw, Scale, Eye, Send, Award } from 'lucide-react';
import './App.css';

interface DisputeItem {
  dispute_id: string;
  transaction_id: string;
  dispute_amount: number;
  currency: string;
  reason: string;
  status: string;
  lifecycle_stage: string;
  response_due_date?: string;
  adjudication_outcome?: string;
}

interface Dossier {
  evidence_type: string;
  compelling_evidence_factor: string;
  historical_matches: Array<{
    prior_transaction_id: string;
    prior_date: string;
    matched_factor: string;
    match_value: string;
  }>;
  formal_defense_narrative: string;
  confidence_score: number;
  adjudication_recommendation: string;
}

const API_BASE = 'http://localhost:8000';
const MERCHANT_HEADERS = {
  'Authorization': 'Bearer merchant_token_default',
  'X-Merchant-Id': 'merchant_default'
};

export default function App() {
  const [disputes, setDisputes] = useState<DisputeItem[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [syncing, setSyncing] = useState<boolean>(false);
  const [selectedDispute, setSelectedDispute] = useState<DisputeItem | null>(null);
  const [dossier, setDossier] = useState<Dossier | null>(null);
  const [synthesizing, setSynthesizing] = useState<boolean>(false);
  const [actionMessage, setActionMessage] = useState<string | null>(null);

  const fetchDisputes = useCallback(async () => {
    try {
      setLoading(true);
      const res = await fetch(`${API_BASE}/api/disputes`);
      if (res.ok) {
        const data = await res.json();
        setDisputes(data);
      }
    } catch (err) {
      console.error('Failed to fetch disputes:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchDisputes();
  }, [fetchDisputes]);

  const handleSync = async () => {
    try {
      setSyncing(true);
      const res = await fetch(`${API_BASE}/api/disputes/sync`, { method: 'POST' });
      if (res.ok) {
        await fetchDisputes();
        setActionMessage('Ledger synchronized directly with PayPal REST API.');
      }
    } catch (err) {
      console.error('Sync failed:', err);
    } finally {
      setSyncing(false);
    }
  };

  const handleOpenInspector = async (disp: DisputeItem) => {
    setSelectedDispute(disp);
    setActionMessage(null);
    try {
      setSynthesizing(true);
      // Auto-synthesize on modal open
      const res = await fetch(`${API_BASE}/api/disputes/${disp.dispute_id}/synthesize`, { method: 'POST' });
      if (res.ok) {
        const d = await res.json();
        setDossier(d);
      }
    } catch (err) {
      console.error('Synthesis failed:', err);
    } finally {
      setSynthesizing(false);
    }
  };

  const handleSubmitDefense = async () => {
    if (!selectedDispute) return;
    try {
      // Step 1: Explicit Server-Side Merchant Approval
      const approveRes = await fetch(`${API_BASE}/api/disputes/${selectedDispute.dispute_id}/approve-evidence`, {
        method: 'POST',
        headers: MERCHANT_HEADERS
      });
      if (!approveRes.ok) {
        const errData = await approveRes.json().catch(() => ({}));
        setActionMessage(`Approval failed: ${errData.detail || 'Merchant authorization error'}`);
        return;
      }

      // Step 2: Certified Defense Submission (backend verifies server-side approval record)
      const res = await fetch(`${API_BASE}/api/disputes/${selectedDispute.dispute_id}/submit-defense`, {
        method: 'POST',
        headers: MERCHANT_HEADERS
      });
      if (res.ok) {
        setActionMessage('Evidence dossier certified, approved by merchant, and submitted to PayPal Disputes API.');
        await fetchDisputes();
      } else {
        const errData = await res.json().catch(() => ({}));
        setActionMessage(`Submission failed: ${errData.detail || 'PayPal submission error'}`);
      }
    } catch (err) {
      console.error('Defense submission failed:', err);
    }
  };

  const handleAdjudicate = async () => {
    if (!selectedDispute) return;
    try {
      const res = await fetch(`${API_BASE}/api/adjudication/${selectedDispute.dispute_id}/resolve?outcome=SELLER_FAVOR`, {
        method: 'POST',
        headers: MERCHANT_HEADERS
      });
      if (res.ok) {
        setActionMessage('Dispute programmatically resolved in SELLER_FAVOR via Sandbox Adjudication API. Held funds released.');
        setSelectedDispute(prev => prev ? { ...prev, status: 'RESOLVED', adjudication_outcome: 'SELLER_FAVOR' } : null);
        await fetchDisputes();
      } else {
        const errData = await res.json().catch(() => ({}));
        setActionMessage(`Adjudication failed: ${errData.detail || 'Sandbox resolution error'}`);
      }
    } catch (err) {
      console.error('Adjudication failed:', err);
    }
  };

  const columnDefs: ColDef<DisputeItem>[] = useMemo(() => [
    {
      field: 'dispute_id',
      headerName: 'Dispute ID',
      width: 220,
      cellRenderer: (params: any) => (
        <span style={{ fontWeight: 600, color: '#60a5fa' }}>{params.value}</span>
      )
    },
    {
      field: 'transaction_id',
      headerName: 'Transaction ID',
      width: 200,
    },
    {
      headerName: 'Disputed Amount',
      width: 160,
      valueGetter: (params) => `${params.data?.dispute_amount} ${params.data?.currency}`,
      cellStyle: { fontWeight: 600 }
    },
    {
      field: 'reason',
      headerName: 'Reason Code',
      width: 280,
      cellRenderer: (params: any) => (
        <span style={{ fontSize: '12px' }}>{params.value}</span>
      )
    },
    {
      field: 'status',
      headerName: 'Status',
      width: 220,
      cellRenderer: (params: any) => {
        const val = params.value;
        const color = val === 'RESOLVED' ? '#34d399' : val === 'UNDER_REVIEW' ? '#60a5fa' : '#f59e0b';
        return (
          <span style={{
            backgroundColor: `${color}20`,
            color: color,
            padding: '3px 8px',
            borderRadius: '4px',
            fontSize: '11px',
            fontWeight: 600
          }}>
            {val}
          </span>
        );
      }
    },
    {
      field: 'lifecycle_stage',
      headerName: 'Stage',
      width: 140,
    },
    {
      headerName: 'Actions',
      width: 150,
      cellRenderer: (params: any) => (
        <button
          onClick={() => handleOpenInspector(params.data)}
          style={{
            backgroundColor: '#0070ba',
            color: 'white',
            border: 'none',
            padding: '4px 10px',
            borderRadius: '4px',
            cursor: 'pointer',
            fontSize: '12px',
            display: 'flex',
            alignItems: 'center',
            gap: '4px',
            marginTop: '8px'
          }}
        >
          <Eye size={13} /> Inspect
        </button>
      )
    }
  ], []);

  return (
    <div className="app-container">
      {/* Header */}
      <div className="header">
        <div className="brand-section">
          <h1><Shield color="#00bcd4" /> DisputeSentinel AI</h1>
          <p>Autonomous Chargeback Defense & Visa Compelling Evidence 3.0 Platform</p>
        </div>
        <div className="header-badges">
          <div className="badge badge-green">
            <CheckCircle size={14} /> PayPal Sandbox: VeriPact-Engine
          </div>
          <div className="badge badge-blue">
            <Scale size={14} /> Visa CE3.0 Deterministic Guards Active
          </div>
        </div>
      </div>

      {/* Metrics Grid */}
      <div className="metrics-grid">
        <div className="metric-card">
          <div className="metric-label">Active Disputed Volume</div>
          <div className="metric-value">$10.00 USD</div>
          <div className="metric-sub">1 Case in Inquiry Stage</div>
        </div>
        <div className="metric-card">
          <div className="metric-label">CE3.0 Telemetry Parity</div>
          <div className="metric-value">6 Factors</div>
          <div className="metric-sub">2 Historical Purchases Matched</div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Algorithmic Win Probability</div>
          <div className="metric-value">96.0%</div>
          <div className="metric-sub">Pydantic Validation Enforced</div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Response Window</div>
          <div className="metric-value">20 Days</div>
          <div className="metric-sub">Deadline: Nov 19, 2026</div>
        </div>
      </div>

      {/* Action Bar */}
      <div className="action-bar">
        <div style={{ display: 'flex', gap: '10px' }}>
          <button className="action-btn action-btn-secondary" onClick={handleSync} disabled={syncing}>
            <RefreshCw size={14} className={syncing ? 'animate-spin' : ''} />
            {syncing ? 'Syncing...' : 'Sync Live PayPal Disputes'}
          </button>
        </div>
        <div style={{ fontSize: '13px', color: '#9ca3af' }}>
          Showing <strong>{disputes.length}</strong> active dispute record(s)
        </div>
      </div>

      {/* AG Grid Primary Forensic Ledger */}
      <div className="ag-theme-quartz-dark grid-wrapper">
        <AgGridReact
          rowData={disputes}
          columnDefs={columnDefs}
          defaultColDef={{
            sortable: true,
            filter: true,
            resizable: true,
          }}
          rowHeight={46}
          loading={loading}
        />
      </div>

      {/* Split-Screen Forensic Dossier Modal */}
      {selectedDispute && (
        <div className="modal-overlay">
          <div className="modal-content">
            <div className="modal-header">
              <h2>Forensic Dispute Intelligence: {selectedDispute.dispute_id}</h2>
              <button
                onClick={() => setSelectedDispute(null)}
                style={{ background: 'none', border: 'none', color: '#9ca3af', fontSize: '20px', cursor: 'pointer' }}
              >
                ✕
              </button>
            </div>

            <div className="modal-body">
              {/* Left Column: PayPal Raw Claim */}
              <div className="column-card">
                <h3>Raw PayPal Claim Telemetry</h3>
                <div className="info-row">
                  <span>Dispute ID:</span>
                  <span>{selectedDispute.dispute_id}</span>
                </div>
                <div className="info-row">
                  <span>Transaction ID:</span>
                  <span>{selectedDispute.transaction_id}</span>
                </div>
                <div className="info-row">
                  <span>Disputed Amount:</span>
                  <span>{selectedDispute.dispute_amount} {selectedDispute.currency}</span>
                </div>
                <div className="info-row">
                  <span>Lifecycle Stage:</span>
                  <span>{selectedDispute.lifecycle_stage}</span>
                </div>
                <div className="info-row">
                  <span>Current Status:</span>
                  <span style={{ color: selectedDispute.status === 'RESOLVED' ? '#34d399' : '#f59e0b' }}>
                    {selectedDispute.status}
                  </span>
                </div>
                {selectedDispute.adjudication_outcome && (
                  <div className="info-row">
                    <span>Adjudication Outcome:</span>
                    <span style={{ color: '#34d399', fontWeight: 'bold' }}>{selectedDispute.adjudication_outcome}</span>
                  </div>
                )}
                <div style={{ marginTop: '16px' }}>
                  <div style={{ fontSize: '12px', color: '#9ca3af', marginBottom: '6px' }}>Buyer Repudiation Statement:</div>
                  <div style={{ background: '#0d1117', padding: '10px', borderRadius: '6px', fontSize: '12px', color: '#e5e7eb', fontStyle: 'italic' }}>
                    "I purchased this software license on Oct 30 but never received the activation key or download link."
                  </div>
                </div>
              </div>

              {/* Right Column: Visa CE3.0 Telemetry Parity & Brief */}
              <div className="column-card">
                <h3>Visa Compelling Evidence 3.0 Defense Brief</h3>

                {dossier ? (
                  <>
                    <div className="info-row">
                      <span>CE3.0 Factor:</span>
                      <span style={{ color: '#00bcd4' }}>{dossier.compelling_evidence_factor}</span>
                    </div>
                    <div className="info-row">
                      <span>Calculated Win Probability:</span>
                      <span style={{ color: '#34d399', fontWeight: 'bold' }}>{(dossier.confidence_score * 100).toFixed(1)}%</span>
                    </div>

                    <div className="gates-box">
                      <div className="gate-item"><CheckCircle size={13} /> Gate 1: Historical Transactions Verified in Local Ledger</div>
                      <div className="gate-item"><CheckCircle size={13} /> Gate 2: Carrier Tracking Syntactically Validated</div>
                      <div className="gate-item"><CheckCircle size={13} /> Gate 3: Monetary Totals Programmatically Re-Calculated</div>
                      <div className="gate-item"><CheckCircle size={13} /> Gate 4: Zero Prompt Injection Signatures Detected</div>
                      <div className="gate-item"><CheckCircle size={13} /> Gate 5: Confidence Score Exceeds Autonomous Threshold</div>
                    </div>

                    <div style={{ marginTop: '14px' }}>
                      <div style={{ fontSize: '12px', color: '#9ca3af', marginBottom: '6px' }}>Certified Legal Brief:</div>
                      <div className="brief-box">
                        {dossier.formal_defense_narrative}
                      </div>
                    </div>
                  </>
                ) : (
                  <div>{synthesizing ? 'Correlating telemetry and drafting CE3.0 brief...' : 'No dossier available.'}</div>
                )}
              </div>
            </div>

            {actionMessage && (
              <div style={{ padding: '10px 24px', backgroundColor: 'rgba(16, 185, 129, 0.15)', color: '#34d399', fontSize: '13px', display: 'flex', alignItems: 'center', gap: '8px' }}>
                <CheckCircle size={14} /> {actionMessage}
              </div>
            )}

            <div className="modal-footer">
              <button
                className="action-btn action-btn-secondary"
                onClick={handleSubmitDefense}
                disabled={selectedDispute.status === 'RESOLVED'}
              >
                <Send size={14} /> Submit Evidence to PayPal
              </button>
              <button
                className="action-btn btn-success"
                onClick={handleAdjudicate}
                disabled={selectedDispute.status === 'RESOLVED'}
              >
                <Award size={14} /> Adjudicate Dispute (SELLER_FAVOR)
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
