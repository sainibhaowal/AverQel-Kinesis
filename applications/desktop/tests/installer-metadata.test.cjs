const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const desktopRoot = path.resolve(__dirname, "..");
const packageConfig = JSON.parse(
  readFileSync(path.join(desktopRoot, "package.json"), "utf8"),
);
const appstream = readFileSync(
  path.join(desktopRoot, "assets/com.averqel.harness.metainfo.xml"),
  "utf8",
);
const frontendBrandRoot = path.resolve(
  desktopRoot,
  "..",
  "..",
  "frontend",
  "public",
  "brand",
);

function readPngDimensions(buffer) {
  assert.equal(buffer.subarray(0, 8).toString("hex"), "89504e470d0a1a0a");
  return {
    width: buffer.readUInt32BE(16),
    height: buffer.readUInt32BE(20),
  };
}

test("keeps the AverQel Kinesis Debian package ID and configures its installer name", () => {
  assert.equal(packageConfig.name, "averqel-harness-desktop");
  assert.equal(packageConfig.build.appId, "com.averqel.harness");
  assert.equal(packageConfig.build.productName, "AverQel Kinesis");
  assert.equal(packageConfig.build.linux.desktop.entry.Name, "AverQel Kinesis");
  assert.ok(
    packageConfig.build.deb.fpm.includes(
      "assets/com.averqel.harness.metainfo.xml=/usr/share/metainfo/com.averqel.harness.metainfo.xml",
    ),
  );
});

test("uses readable ASCII installer text and matching AppStream identity", () => {
  const { synopsis, description } = packageConfig.build.linux;
  assert.doesNotMatch(synopsis, /[^\x00-\x7f]/);
  assert.doesNotMatch(description, /[^\x00-\x7f]/);
  assert.match(appstream, /<id>com\.averqel\.harness<\/id>/);
  assert.match(appstream, /<name>AverQel Kinesis<\/name>/);
  assert.match(
    appstream,
    /<launchable type="desktop-id">AverQel Kinesis\.desktop<\/launchable>/,
  );
});

test("ships the AntiAtom mark in every desktop icon format", () => {
  const iconPng = readFileSync(path.join(desktopRoot, "assets/icon.png"));
  const iconIco = readFileSync(path.join(desktopRoot, "assets/icon.ico"));
  const iconIcns = readFileSync(path.join(desktopRoot, "assets/icon.icns"));
  const dimensions = readPngDimensions(iconPng);

  assert.deepEqual(dimensions, { width: 512, height: 512 });
  assert.equal(iconIco.subarray(0, 4).toString("hex"), "00000100");
  assert.equal(iconIcns.subarray(0, 4).toString("ascii"), "icns");
});

test("stages the canonical AntiAtom SVG brand source in the desktop package", () => {
  assert.equal(
    packageConfig.scripts["prepare:brand-assets"],
    "node scripts/prepare-brand-assets.mjs",
  );
  for (const fileName of ["AverQel_Text.svg", "AntiAtom_Icon.svg"]) {
    assert.deepEqual(
      readFileSync(path.join(desktopRoot, "assets", "brand", fileName)),
      readFileSync(path.join(frontendBrandRoot, fileName)),
      `${fileName} must be copied from the canonical frontend brand source`,
    );
  }
});
