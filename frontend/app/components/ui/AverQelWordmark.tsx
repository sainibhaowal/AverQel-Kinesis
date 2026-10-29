import { PRODUCT_NAME, PRODUCT_SHORT_NAME } from "@/lib/brand";

type AverQelWordmarkProps = {
  size?: "hero" | "nav" | "footer" | "mobile";
  className?: string;
};

const TEXT_LOGO_SOURCE = "/brand/AverQel_Text.svg";

const dimensions = {
  hero: { width: 266, height: 49 },
  nav: { width: 156, height: 29 },
  footer: { width: 108, height: 21 },
  mobile: { width: 114, height: 22 },
} as const;

/** Shared AverQel wordmark with Kinesis shown on a separate second line. */
export default function AverQelWordmark({ size = "nav", className = "" }: AverQelWordmarkProps) {
  const { width, height } = dimensions[size];

  return (
    <span
      className={`averqel-wordmark averqel-wordmark--${size} ${className}`.trim()}
      role="img"
      aria-label={PRODUCT_NAME}
    >
      <svg
        className="averqel-wordmark__svg"
        width={width}
        height={height}
        viewBox="0 0 1371 254"
        aria-hidden="true"
        focusable="false"
        preserveAspectRatio="xMidYMid meet"
      >
        <image
          href={TEXT_LOGO_SOURCE}
          width="1371"
          height="254"
          preserveAspectRatio="xMidYMid meet"
        />
      </svg>
      <span className="forge-wordmark__text">{PRODUCT_SHORT_NAME}</span>
    </span>
  );
}
