/* Small, dependency-free Python highlighter for the processor source overlay. */
const KEYWORDS = new Set([
  "and", "as", "assert", "async", "await", "break", "case", "class", "continue",
  "def", "del", "elif", "else", "except", "False", "finally", "for", "from",
  "global", "if", "import", "in", "is", "lambda", "match", "None", "nonlocal",
  "not", "or", "pass", "raise", "return", "True", "try", "while", "with", "yield",
]);
const BUILTINS = new Set([
  "abs", "all", "any", "bool", "bytes", "callable", "chr", "dict", "dir", "enumerate",
  "filter", "float", "format", "frozenset", "getattr", "hasattr", "hash", "help", "hex",
  "id", "input", "int", "isinstance", "issubclass", "iter", "len", "list", "map", "max",
  "memoryview", "min", "next", "object", "oct", "open", "ord", "pow", "print", "property",
  "range", "repr", "reversed", "round", "set", "setattr", "slice", "sorted", "str", "sum",
  "super", "tuple", "type", "vars", "zip", "self",
]);
const NUMBER = /^(?:0[xX][0-9a-fA-F_]+|0[oO][0-7_]+|0[bB][01_]+|(?:\d[\d_]*(?:\.[\d_]*)?|\.[\d_]+)(?:[eE][+-]?[\d_]+)?[jJ]?)/;
const IDENTIFIER = /^[A-Za-z_]\w*/;
const DECORATOR = /^@[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*/;
const OPERATOR = /^[+\-*/%@&|^~:=<>!]+/;
const STRING_PREFIX = /^[rRuUbBfF]{1,3}(?=["'])/;
export const PYTHON_HIGHLIGHT_MAX_CHARS = 256 * 1024;

function stringStart(source, index) {
  let quoteAt = index;
  if (source[index] !== '"' && source[index] !== "'") {
    if (index > 0 && /[A-Za-z0-9_]/.test(source[index - 1])) return null;
    const prefix = STRING_PREFIX.exec(source.slice(index, index + 4));
    if (!prefix) return null;
    quoteAt += prefix[0].length;
  }
  const quote = source[quoteAt];
  if (quote !== '"' && quote !== "'") return null;
  const triple = source.slice(quoteAt, quoteAt + 3) === quote.repeat(3);
  return { quoteAt, delimiter: triple ? quote.repeat(3) : quote };
}

function stringEnd(source, start, descriptor) {
  let at = descriptor.quoteAt + descriptor.delimiter.length;
  while (at < source.length) {
    if (source.startsWith(descriptor.delimiter, at)) return at + descriptor.delimiter.length;
    if (source[at] === "\\") at += Math.min(2, source.length - at);
    else at += 1;
  }
  return source.length;
}

function appendToken(tokens, type, text) {
  if (!text) return;
  const previous = tokens.at(-1);
  if (previous?.type === type) previous.text += text;
  else tokens.push({ type, text });
}

export function pythonHighlightTokens(value) {
  const source = String(value ?? "");
  if (source.length > PYTHON_HIGHLIGHT_MAX_CHARS) return [{ type: "plain", text: source }];
  const tokens = [];
  let at = 0;
  let expectDefinition = false;
  while (at < source.length) {
    const rest = source.slice(at);
    const string = stringStart(source, at);
    if (string) {
      const end = stringEnd(source, at, string);
      appendToken(tokens, "string", source.slice(at, end));
      at = end;
      expectDefinition = false;
      continue;
    }
    if (source[at] === "#") {
      const newline = source.indexOf("\n", at);
      const end = newline < 0 ? source.length : newline;
      appendToken(tokens, "comment", source.slice(at, end));
      at = end;
      continue;
    }
    const decorator = DECORATOR.exec(rest);
    if (decorator) {
      appendToken(tokens, "decorator", decorator[0]);
      at += decorator[0].length;
      continue;
    }
    const number = NUMBER.exec(rest);
    if (number) {
      appendToken(tokens, "number", number[0]);
      at += number[0].length;
      expectDefinition = false;
      continue;
    }
    const identifier = IDENTIFIER.exec(rest);
    if (identifier) {
      const word = identifier[0];
      let type = "plain";
      if (expectDefinition) type = "definition";
      else if (KEYWORDS.has(word)) type = "keyword";
      else if (BUILTINS.has(word)) type = "builtin";
      appendToken(tokens, type, word);
      expectDefinition = word === "def" || word === "class";
      at += word.length;
      continue;
    }
    const operator = OPERATOR.exec(rest);
    if (operator) {
      appendToken(tokens, "operator", operator[0]);
      at += operator[0].length;
      continue;
    }
    appendToken(tokens, "plain", source[at]);
    if (source[at] === "\n") expectDefinition = false;
    at += 1;
  }
  return tokens;
}

export function renderPythonHighlight(target, value) {
  if (!target) return;
  const source = String(value ?? "");
  const doc = target.ownerDocument || document;
  const fragment = doc.createDocumentFragment();
  for (const token of pythonHighlightTokens(source)) {
    if (token.type === "plain") fragment.append(doc.createTextNode(token.text));
    else {
      const span = doc.createElement("span");
      span.className = `py-${token.type}`;
      span.textContent = token.text;
      fragment.append(span);
    }
  }
  if (source.endsWith("\n")) fragment.append(doc.createTextNode(" "));
  target.replaceChildren(fragment);
}
