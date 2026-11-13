import type { HTMLAttributes, CSSProperties } from "react";

type LoaderProps = Omit<HTMLAttributes<HTMLSpanElement>, "children"> & {
  size?: number;
  label?: string;
};

function sizeFromClassName(className: string): number | undefined {
  const match = className.match(/(?:^|\s)(?:h|w)-(\d+(?:\.\d+)?)(?:\s|$)/);
  return match ? Number(match[1]) * 4 : undefined;
}

export function AverQelLoader({ size, label, className = "", ...spanProps }: LoaderProps) {
  const resolvedSize = size ?? sizeFromClassName(className) ?? 24;
  const cleanClassName = className.replace(/\banimate-spin\b/g, "").trim();
  const style = {
    width: `${resolvedSize}px`,
    height: `${resolvedSize}px`,
  } as CSSProperties;

  return (
    <span
      {...spanProps}
      className={`averqel-loading-indicator ${cleanClassName}`.trim()}
      style={style}
      role={label ? "status" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      <span className="averqel-loading-indicator__wrapper" aria-hidden="true">
        <span className="averqel-loading-indicator__box-wrap">
          <span className="averqel-loading-indicator__box one" />
          <span className="averqel-loading-indicator__box two" />
          <span className="averqel-loading-indicator__box three" />
          <span className="averqel-loading-indicator__box four" />
          <span className="averqel-loading-indicator__box five" />
          <span className="averqel-loading-indicator__box six" />
        </span>
      </span>
    </span>
  );
}

type LoadingLayoutProps = {
  label: string;
  className?: string;
};

type LoadingScreenProps = {
  label?: string;
  className?: string;
};

export function AverQelLoadingStatus({ label, className = "" }: LoadingLayoutProps) {
  return (
    <div className={`averqel-loading-status ${className}`.trim()} role="status" aria-live="polite">
      <AverQelLoader size={40} className="text-primary" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function AverQelLoadingScreen({
  label = "Loading AverQel",
  className = "",
}: LoadingScreenProps) {
  return (
    <div className={`averqel-loading-screen ${className}`.trim()} role="status" aria-live="polite">
      <span className="averqel-loading-screen__brand">AVERQEL</span>
      <AverQelLoader size={200} className="text-primary" aria-hidden="true" />
      <span className="averqel-loading-screen__label">{label}</span>
    </div>
  );
}
