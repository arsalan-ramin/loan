import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import InputReadiness from './InputReadiness';

describe('Input readiness', () => {
  it('does not label mapped bankruptcy evidence as a missing column', () => {
    render(<InputReadiness items={['BANKRUPTCY_5_CALENDAR_YEARS_DIAGNOSTIC', 'explicit_iso_as_of_date', 'report_selection_requires_explicit_as_of_date']} rules={[{
      rule_id: 'BANKRUPTCY_5_CALENDAR_YEARS_DIAGNOSTIC', status: 'UNKNOWN',
      detail: 'Bankruptcy count is mapped, but no report was selected.', evidence_refs: ['cell-1'], page: 2, policy_reference: null,
    }]} />);
    const region = screen.getByRole('region', { name: 'Input and check readiness' });
    expect(within(region).queryByRole('heading', { name: 'Missing inputs' })).not.toBeInTheDocument();
    expect(screen.getByText('Bankruptcy count is mapped, but no report was selected.')).toBeInTheDocument();
    expect(screen.getByText(/not by missing bureau columns/)).toBeInTheDocument();
    expect(screen.getByText(/1 mapped source evidence references/)).toBeInTheDocument();
    const dataList = within(region).getByRole('list');
    expect(dataList).not.toHaveTextContent('BANKRUPTCY_5_CALENDAR_YEARS_DIAGNOSTIC');
  });

  it('keeps legacy bounded-inspection rule IDs out of missing-data requirements', () => {
    render(<InputReadiness items={['CREDIT_FREEZE_CLEAR', 'sbss_response_id_not_exported']} rules={[]} />);
    expect(screen.getByText('CREDIT_FREEZE_CLEAR')).toBeInTheDocument();
    expect(screen.getByRole('list')).not.toHaveTextContent('CREDIT_FREEZE_CLEAR');
    expect(screen.getByText(/separately loan-linked SBSS score may still be available/)).toBeInTheDocument();
  });
});