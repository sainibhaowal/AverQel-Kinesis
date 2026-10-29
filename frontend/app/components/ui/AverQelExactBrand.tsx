import Image from "next/image";
import { PRODUCT_NAME, PRODUCT_SHORT_NAME } from "@/lib/brand";

/** Shared AntiAtom lockup with the original AverQel wordmark above Kinesis. */
export default function AverQelExactBrand() {
  return (
    <span className="averqel-forge-login-lockup" role="img" aria-label={PRODUCT_NAME}>
      <Image
        src="/brand/AntiAtom_Icon.svg"
        alt=""
        width={144}
        height={144}
        priority
        sizes="144px"
        className="averqel-forge-login-lockup__icon"
      />
      <span className="averqel-forge-login-lockup__wordmark">
        <Image
          src="/brand/AverQel_Text.svg"
          alt=""
          width={266}
          height={49}
          priority
          sizes="(max-width: 640px) 46vw, 266px"
          className="averqel-forge-login-lockup__averqel"
        />
        <span className="averqel-forge-login-lockup__forge">{PRODUCT_SHORT_NAME}</span>
      </span>
    </span>
  );
}
