import { readdirSync, statSync } from "node:fs";
import path from "node:path";

const MODEL_WEIGHT_PATTERNS = [
  /\.safetensors(?:\.index\.json)?$/i,
  /^pytorch_model(?:\.[^.]+)?\.bin$/i,
  /^model(?:\.[^.]+)?\.bin$/i,
  /^model(?:\.[^.]+)?\.onnx$/i,
  /^tf_model.*\.h5$/i,
  /^flax_model.*\.msgpack$/i,
  /^model.*\.(?:ckpt|pth)$/i,
];

function walkFiles(root) {
  const files = [];
  const visit = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const entryPath = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        visit(entryPath);
      } else if (entry.isFile()) {
        files.push(entryPath);
      }
    }
  };
  visit(root);
  return files;
}

export function bundledModelWeights(root) {
  return walkFiles(root).filter((filePath) => {
    const fileName = path.basename(filePath);
    return MODEL_WEIGHT_PATTERNS.some((pattern) => pattern.test(fileName));
  });
}

export function auditBackendBundle(root) {
  if (!statSync(root).isDirectory()) {
    throw new Error(`Packaged backend directory is missing: ${root}`);
  }
  const bundledWeights = bundledModelWeights(root);
  if (bundledWeights.length > 0) {
    throw new Error(
      `Refusing to package Hugging Face/model weights inside the desktop backend:\n${bundledWeights.join("\n")}`,
    );
  }
}
