/**
 * Tests for useScrollProgress, with a mocked requestAnimationFrame and a
 * mocked element rect (jsdom has neither layout nor a frame clock).
 */

import { render } from '@testing-library/react';
import { useRef } from 'react';

import useScrollProgress, { HANDOFF_REST_TOP, clamp01, progressFor } from './useScrollProgress.js';

describe('progressFor', () => {
  it('travel: 0 when the top reaches the viewport bottom, 1 when the bottom leaves the top', () => {
    expect(progressFor({ top: 800, height: 400 }, 800, 'travel')).toBe(0);
    expect(progressFor({ top: -400, height: 400 }, 800, 'travel')).toBe(1);
    expect(progressFor({ top: 200, height: 400 }, 800, 'travel')).toBeCloseTo(0.5);
  });

  it('pin: 0 at the top of the viewport, 1 when the bottom reaches the bottom', () => {
    expect(progressFor({ top: 0, height: 2800 }, 800, 'pin')).toBe(0);
    expect(progressFor({ top: -1000, height: 2800 }, 800, 'pin')).toBeCloseTo(0.5);
    expect(progressFor({ top: -2000, height: 2800 }, 800, 'pin')).toBe(1);
    expect(progressFor({ top: 300, height: 2800 }, 800, 'pin')).toBe(0);
  });

  it('handoff: driven by scrollY toward the target resting at restTop', () => {
    expect(progressFor(null, 800, 'handoff', { scrollY: 0, targetTop: 1200, restTop: 200 })).toBe(0);
    expect(progressFor(null, 800, 'handoff', { scrollY: 500, targetTop: 1200, restTop: 200 })).toBeCloseTo(0.5);
    expect(progressFor(null, 800, 'handoff', { scrollY: 1000, targetTop: 1200, restTop: 200 })).toBe(1);
    // Default rest, and a target already above its rest point.
    expect(progressFor(null, 800, 'handoff', { scrollY: 0, targetTop: HANDOFF_REST_TOP })).toBe(1);
  });

  it('clamps', () => {
    expect(clamp01(-2)).toBe(0);
    expect(clamp01(5)).toBe(1);
  });
});

describe('useScrollProgress', () => {
  let frames;
  let rectTop;

  beforeEach(() => {
    frames = [];
    rectTop = 400;
    window.requestAnimationFrame = jest.fn((cb) => {
      frames.push(cb);
      return frames.length;
    });
    window.cancelAnimationFrame = jest.fn((id) => {
      frames[id - 1] = null;
    });
    Object.defineProperty(window, 'innerHeight', { value: 800, configurable: true });
    jest.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(() => ({
      top: rectTop,
      height: 400,
      bottom: rectTop + 400,
      left: 0,
      right: 100,
      width: 100,
    }));
  });

  afterEach(() => {
    jest.restoreAllMocks();
    delete window.requestAnimationFrame;
    delete window.cancelAnimationFrame;
  });

  const flush = () => {
    const pending = frames;
    frames = [];
    pending.forEach((cb) => cb && cb(0));
  };

  function Probe({ enabled = true, onProgress, mode }) {
    const ref = useRef(null);
    useScrollProgress(ref, { enabled, onProgress, mode, restValue: 0.25 });
    return <div ref={ref} data-testid="probe" />;
  }

  it('sets --p from the element position on the next animation frame', () => {
    const onProgress = jest.fn();
    const { getByTestId } = render(<Probe onProgress={onProgress} />);
    expect(getByTestId('probe').style.getPropertyValue('--p')).toBe('');
    flush();
    // top 400 in an 800px viewport, height 400: (800 - 400) / 1200.
    expect(getByTestId('probe').style.getPropertyValue('--p')).toBe('0.3333');
    expect(onProgress).toHaveBeenCalledWith(expect.closeTo(1 / 3, 4));
  });

  it('coalesces a burst of scroll events into one frame, and reads layout once per frame', () => {
    render(<Probe />);
    flush();
    HTMLElement.prototype.getBoundingClientRect.mockClear();
    rectTop = 0;
    for (let i = 0; i < 10; i += 1) window.dispatchEvent(new Event('scroll'));
    expect(frames).toHaveLength(1);
    flush();
    expect(HTMLElement.prototype.getBoundingClientRect).toHaveBeenCalledTimes(1);
  });

  it('skips the write when progress has not changed', () => {
    const onProgress = jest.fn();
    render(<Probe onProgress={onProgress} />);
    flush();
    window.dispatchEvent(new Event('scroll'));
    flush();
    expect(onProgress).toHaveBeenCalledTimes(1);
  });

  it('does nothing while the tab is hidden, and catches up when it is shown', () => {
    const onProgress = jest.fn();
    const hidden = jest.spyOn(document, 'hidden', 'get').mockReturnValue(true);
    render(<Probe onProgress={onProgress} />);
    flush();
    expect(onProgress).not.toHaveBeenCalled();
    hidden.mockReturnValue(false);
    document.dispatchEvent(new Event('visibilitychange'));
    flush();
    expect(onProgress).toHaveBeenCalledTimes(1);
  });

  it('when disabled, attaches nothing and shows the rest value', () => {
    const add = jest.spyOn(window, 'addEventListener');
    const { getByTestId } = render(<Probe enabled={false} />);
    expect(getByTestId('probe').style.getPropertyValue('--p')).toBe('0.25');
    expect(add).not.toHaveBeenCalledWith('scroll', expect.anything(), expect.anything());
    expect(window.requestAnimationFrame).not.toHaveBeenCalled();
  });

  it('removes its listeners and cancels a pending frame on unmount', () => {
    const remove = jest.spyOn(window, 'removeEventListener');
    const { unmount } = render(<Probe />);
    // A frame is pending from the initial schedule.
    expect(frames).toHaveLength(1);
    unmount();
    expect(window.cancelAnimationFrame).toHaveBeenCalled();
    expect(remove).toHaveBeenCalledWith('scroll', expect.any(Function));
    expect(remove).toHaveBeenCalledWith('resize', expect.any(Function));
    // A scroll after unmount schedules nothing.
    window.dispatchEvent(new Event('scroll'));
    expect(frames.filter(Boolean)).toHaveLength(0);
  });
});
