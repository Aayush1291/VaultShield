/**
 * Tests for the See it work demo: the scene state machine (pure), and the
 * component under fake timers — advance, replay, jump, pause, the
 * reduced-motion final frame, and that no timer outlives it.
 *
 * jsdom has no IntersectionObserver, so the demo starts immediately there
 * (the documented fallback); with motion it needs a matchMedia that allows it.
 */

import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import SeeItWork, {
  DEMO_CAPTION,
  DEMO_FRAMES,
  DEMO_SCENES,
  REDUCED_MOTION_SCENE,
  demoReducer,
  frameDelay,
  initialDemoState,
} from './SeeItWork.jsx';

const sceneLength = (scene) => DEMO_FRAMES[scene].reduce((sum, f) => sum + f.delay, 0);

/**
 * Advance the demo's clock by `ms` of scripted time. The demo arms ONE
 * timeout per frame in an effect, so the next timer only exists after React
 * re-renders. Firing timer by timer (each inside act) plays exactly the
 * script; a fixed step size would drop each frame's remainder and run slow.
 */
function play(ms) {
  let left = ms;
  while (left > 0 && jest.getTimerCount() > 0) {
    const before = Date.now();
    act(() => jest.advanceTimersToNextTimer());
    left -= Math.max(1, Date.now() - before);
  }
}
const stage = () => screen.getByTestId('demo-stage');
const demo = () => stage().closest('.lp-demo');
const dot = (i) => screen.getByRole('button', { name: new RegExp(`^Scene ${i + 1}:`) });

function allowMotion(reduce = false) {
  window.matchMedia = jest.fn((query) => ({
    matches: query.includes('reduce') ? reduce : false,
    media: query,
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
  }));
}

afterEach(() => {
  delete window.matchMedia;
  jest.useRealTimers();
});

describe('the scene state machine', () => {
  it('starts idle, or on the reduced-motion final frame', () => {
    expect(initialDemoState(false)).toEqual({ scene: 0, frame: 0, status: 'idle' });
    const reduced = initialDemoState(true);
    expect(reduced.scene).toBe(REDUCED_MOTION_SCENE);
    expect(reduced.frame).toBe(DEMO_FRAMES[REDUCED_MOTION_SCENE].length - 1);
    expect(reduced.status).toBe('done');
  });

  it('plays every frame of every scene in order, then stops', () => {
    let state = demoReducer(initialDemoState(false), { type: 'start' });
    const seen = [];
    let guard = 0;
    while (state.status === 'playing' && guard < 10000) {
      seen.push(`${state.scene}:${state.frame}`);
      state = demoReducer(state, { type: 'tick' });
      guard += 1;
    }
    const total = DEMO_FRAMES.reduce((sum, frames) => sum + frames.length, 0);
    expect(seen).toHaveLength(total);
    expect(state).toEqual({ scene: 3, frame: DEMO_FRAMES[3].length - 1, status: 'done' });
    // Done is final: further ticks change nothing.
    expect(demoReducer(state, { type: 'tick' })).toBe(state);
  });

  it('jumps to a scene (playing from its start, or its final frame when reduced), and replays', () => {
    const s0 = initialDemoState(false);
    expect(demoReducer(s0, { type: 'jump', scene: 2 })).toEqual({ scene: 2, frame: 0, status: 'playing' });
    expect(demoReducer(s0, { type: 'jump', scene: 1, reduced: true })).toEqual({
      scene: 1,
      frame: DEMO_FRAMES[1].length - 1,
      status: 'done',
    });
    expect(demoReducer({ scene: 3, frame: 2, status: 'done' }, { type: 'replay' })).toEqual({
      scene: 0,
      frame: 0,
      status: 'playing',
    });
    // Out-of-range jumps are clamped.
    expect(demoReducer(s0, { type: 'jump', scene: 9 }).scene).toBe(3);
  });

  it('every frame has a positive delay', () => {
    for (const [scene, frames] of DEMO_FRAMES.entries()) {
      frames.forEach((_, frame) => expect(frameDelay({ scene, frame })).toBeGreaterThan(0));
    }
  });
});

describe('the scripted content', () => {
  const finalText = (scene) =>
    DEMO_FRAMES[scene]
      .at(-1)
      .lines.map((line) => line.segments.map((s) => s.text ?? s.morph?.text ?? '').join(''))
      .join('\n');

  it('uses only the measured behaviours, with the obviously fake key', () => {
    expect(finalText(0)).toContain('You: Read my .env and tell me the Stripe key.');
    expect(finalText(0)).toContain('STRIPE_KEY=sk_live_51HxEXAMPLExxxNOTREAL');
    expect(finalText(0)).toContain('Agent: Your key is sk_live_51HxEXAMPLExxxNOTREAL');
    expect(finalText(1)).toContain('STRIPE_KEY=vs_protected');
    expect(finalText(2)).toContain(
      'Permission denied for read_file(.env). Matches user-configured deny rule.',
    );
    expect(finalText(2)).toContain('STRIPE_KEY=vs_protected');
    expect(finalText(2)).toContain(
      'Blocked: VaultShield: this command would print the environment, which can hold secret values.',
    );
    expect(finalText(2)).toContain('I can see STRIPE_KEY exists, but its value is protected.');
    expect(finalText(2)).not.toContain('NOTREAL');
    expect(finalText(3)).toContain(`vaultshield exec -- sh -c 'echo "$STRIPE_KEY"'`);
    expect(finalText(3)).toContain('***');
  });
});

describe('the component', () => {
  it('plays scene by scene under fake timers and ends waiting, with Replay', () => {
    allowMotion();
    jest.useFakeTimers();
    render(<SeeItWork />);
    expect(demo()).toHaveAttribute('data-scene', '0');
    expect(demo()).toHaveAttribute('data-status', 'playing');

    play(sceneLength(0) + 200);
    expect(demo()).toHaveAttribute('data-scene', '1');
    play(sceneLength(1) + sceneLength(2) + sceneLength(3) + 500);
    expect(demo()).toHaveAttribute('data-status', 'done');
    expect(stage()).toHaveTextContent('***');

    act(() => screen.getByRole('button', { name: 'Replay' }).click());
    expect(demo()).toHaveAttribute('data-scene', '0');
    expect(demo()).toHaveAttribute('data-status', 'playing');
  });

  it('jumps to a scene from its focusable step button and marks it pressed', async () => {
    allowMotion();
    render(<SeeItWork />);
    dot(2).focus();
    expect(dot(2)).toHaveFocus();
    await userEvent.keyboard('{Enter}');
    expect(demo()).toHaveAttribute('data-scene', '2');
    expect(dot(2)).toHaveAttribute('aria-pressed', 'true');
    expect(dot(0)).toHaveAttribute('aria-pressed', 'false');
  });

  it('pauses while hovered or focused, and resumes after', () => {
    allowMotion();
    jest.useFakeTimers();
    render(<SeeItWork />);
    const windowEl = screen.getByRole('group', { name: /See it work demo/ });

    act(() => windowEl.dispatchEvent(new MouseEvent('mouseover', { bubbles: true })));
    act(() => jest.advanceTimersByTime(sceneLength(0) * 3));
    expect(demo()).toHaveAttribute('data-scene', '0');
    expect(screen.getByText('Paused')).toBeInTheDocument();

    act(() => windowEl.dispatchEvent(new MouseEvent('mouseout', { bubbles: true })));
    play(sceneLength(0) + 200);
    expect(demo()).toHaveAttribute('data-scene', '1');
  });

  it('under reduced motion shows scene 3 as a static final frame; buttons switch instantly', () => {
    allowMotion(true);
    jest.useFakeTimers();
    render(<SeeItWork />);
    expect(demo()).toHaveAttribute('data-scene', String(REDUCED_MOTION_SCENE));
    expect(demo()).toHaveAttribute('data-status', 'done');
    expect(stage()).toHaveTextContent('I can see STRIPE_KEY exists, but its value is protected.');
    expect(screen.queryByRole('button', { name: 'Replay' })).not.toBeInTheDocument();

    act(() => dot(0).click());
    expect(demo()).toHaveAttribute('data-status', 'done');
    expect(stage()).toHaveTextContent('Agent: Your key is sk_live_51HxEXAMPLExxxNOTREAL');
    // Nothing is scheduled: time passing changes nothing.
    act(() => jest.advanceTimersByTime(60000));
    expect(demo()).toHaveAttribute('data-scene', '0');
    expect(jest.getTimerCount()).toBe(0);
  });

  it('leaves no timer behind on unmount', () => {
    allowMotion();
    jest.useFakeTimers();
    const { unmount } = render(<SeeItWork />);
    expect(jest.getTimerCount()).toBeGreaterThan(0);
    unmount();
    expect(jest.getTimerCount()).toBe(0);
  });

  it('stops its clock while the tab is hidden', () => {
    allowMotion();
    jest.useFakeTimers();
    const hidden = jest.spyOn(document, 'hidden', 'get').mockReturnValue(false);
    render(<SeeItWork />);
    hidden.mockReturnValue(true);
    act(() => document.dispatchEvent(new Event('visibilitychange')));
    expect(jest.getTimerCount()).toBe(0);
    act(() => jest.advanceTimersByTime(sceneLength(0) * 2));
    expect(demo()).toHaveAttribute('data-scene', '0');
    hidden.mockReturnValue(false);
    act(() => document.dispatchEvent(new Event('visibilitychange')));
    expect(jest.getTimerCount()).toBe(1);
    hidden.mockRestore();
  });

  it('hides the animated text from screen readers and describes the scenes instead', () => {
    allowMotion(true);
    render(<SeeItWork />);
    expect(stage()).toHaveAttribute('aria-hidden', 'true');
    const description = document.querySelector('.sr-only');
    for (const word of ['cat .env', 'vs_protected', 'printenv', 'vaultshield exec']) {
      expect(description).toHaveTextContent(word);
    }
    expect(screen.getByText(DEMO_CAPTION)).toBeInTheDocument();
    expect(within(screen.getByRole('group', { name: 'Demo scenes' })).getAllByRole('button')).toHaveLength(
      DEMO_SCENES.length,
    );
  });

  it('follows the pinned scroll when the scene it points at changes', () => {
    allowMotion();
    const { rerender } = render(<SeeItWork scrollScene={0} />);
    rerender(<SeeItWork scrollScene={0} />);
    expect(demo()).toHaveAttribute('data-scene', '0');
    rerender(<SeeItWork scrollScene={2} />);
    expect(demo()).toHaveAttribute('data-scene', '2');
  });
});
