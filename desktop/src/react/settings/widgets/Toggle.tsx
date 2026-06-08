/**
 * .hana-toggle 复用：方角滑块。
 *
 * 三态：
 * - on=true  开
 * - on=false 关
 * - on=undefined  数据未加载（视觉为中性 + disabled，点击不会触发 onChange）
 *
 * 异步加载的开关一律传 undefined 兜底，避免 fetch 完成前显示"假关"
 * 导致用户在加载窗口期误触发。
 */
import React from 'react';

interface ToggleProps {
  on: boolean | undefined;
  onChange: (on: boolean) => void;
  label?: string;
  disabled?: boolean;
}

export function Toggle({ on, onChange, label, disabled = false }: ToggleProps) {
  const loading = on === undefined;
  const visualOn = on === true;
  const effectiveDisabled = disabled || loading;
  const className = [
    'hana-toggle',
    visualOn ? 'on' : '',
    loading ? 'loading' : '',
  ].filter(Boolean).join(' ');
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <button
        className={className}
        type="button"
        disabled={effectiveDisabled}
        aria-label={label}
        aria-busy={loading || undefined}
        role="switch"
        aria-checked={loading ? 'mixed' : visualOn}
        onClick={(e) => {
          e.stopPropagation();
          if (loading) return;
          onChange(!visualOn);
        }}
      />
      {label && <span className="hana-toggle-label">{label}</span>}
    </div>
  );
}
