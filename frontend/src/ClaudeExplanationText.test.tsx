import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import ClaudeExplanationText from './ClaudeExplanationText';

describe('Claude explanation formatting', () => {
  it('formats labeled sections and numbered blockers as bullets', () => {
    render(<ClaudeExplanationText text={'Result: Review required.\nWhy: SBSS is 155.\nWhat is unresolved: (1) Missing dates. (2) Conflicting scores.\nNext steps: Correct source evidence.'} />);
    expect(screen.getAllByRole('heading')).toHaveLength(4);
    expect(screen.getAllByRole('listitem')).toHaveLength(5);
    expect(screen.getByText('Missing dates.')).toBeInTheDocument();
    expect(screen.getByText('Conflicting scores.')).toBeInTheDocument();
  });
  it('preserves legacy text and escapes model content', () => {
    const text = '<script>alert(1)</script>\nOriginal evidence';
    const { container } = render(<ClaudeExplanationText text={text} />);
    expect(screen.getByText('<script>alert(1)</script>')).toBeInTheDocument();
    expect(container.querySelector('script')).toBeNull();
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
  });
});