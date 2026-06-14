import { expect, test } from "vitest";
import { stockDetailPath, stockDetailRouteSymbol } from "../stockDetailPath";

test("stock detail path canonicalizes bare crypto symbols to KRW market codes", () => {
  expect(stockDetailPath("CRYPTO", "BTC")).toBe("/stocks/crypto/KRW-BTC");
  expect(stockDetailPath("CRYPTO", "btc")).toBe("/stocks/crypto/KRW-BTC");
  expect(stockDetailPath("CRYPTO", "BTC-KRW")).toBe("/stocks/crypto/KRW-BTC");
  expect(stockDetailPath("CRYPTO", "KRW-BTC")).toBe("/stocks/crypto/KRW-BTC");
});

test("stock detail path preserves KR and US symbols", () => {
  expect(stockDetailPath("KR", "005930")).toBe("/stocks/kr/005930");
  expect(stockDetailPath("US", "BRK-B")).toBe("/stocks/us/BRK-B");
});

test("stock detail route symbol supports lowercase route market keys for recent symbols", () => {
  expect(stockDetailRouteSymbol("crypto", "BTC")).toBe("KRW-BTC");
  expect(stockDetailRouteSymbol("crypto", "btc-krw")).toBe("KRW-BTC");
  expect(stockDetailRouteSymbol("us", "BRK-B")).toBe("BRK-B");
});
