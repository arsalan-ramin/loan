import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import RulesDialog from './RulesDialog';
import type { Rule } from './types';

const rule: Rule = { id: 'word-count', name: 'Minimum words', kind: 'min_words', value: '250', severity: 'review', enabled: true };

it.each(['0', '-1', '1000001', '1.5', '9007199254740992', ''])('rejects minimum word count %s without calling the API', async (value) => {
  const onSave = vi.fn();
  render(<RulesDialog rules={[{ ...rule, value }]} onSave={onSave} onClose={vi.fn()} />);
  const dialog = screen.getByRole('dialog');
  expect(within(dialog).getByText('Tell Agent what matters. Enabled rules are checked when you process your documents.')).toBeInTheDocument();
  const input = within(dialog).getByLabelText('Minimum word count');
  expect(input).toHaveAttribute('min', '1');
  expect(input).toHaveAttribute('max', '1000000');
  expect(input).toHaveAttribute('step', '1');
  // Bypass native constraints to also verify the submit handler's validation.
  fireEvent.submit(dialog.querySelector('form')!);
  expect(await screen.findByRole('alert')).toHaveTextContent('Minimum words must be a whole number from 1 to 1,000,000.');
  expect(onSave).not.toHaveBeenCalled();
});

it.each(['1', '1000000'])('saves valid minimum word count boundary %s', async (value) => {
  const onSave = vi.fn().mockResolvedValue(undefined);
  const onClose = vi.fn();
  render(<RulesDialog rules={[{ ...rule, value }]} onSave={onSave} onClose={onClose} />);
  const dialog = screen.getByRole('dialog');
  expect(within(dialog).getByLabelText('Minimum word count')).toBeValid();
  fireEvent.submit(dialog.querySelector('form')!);
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
  expect(onSave).toHaveBeenCalledWith([{ ...rule, value }]);
});