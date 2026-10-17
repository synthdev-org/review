// Compact JSON formatter for adapter/analyzers/eslint.py. The built-in `json`
// formatter echoes each file's full source (and fix suggestions) back, which
// could blow the analyzer's 10 MB output cap on a large file.
module.exports = (results) =>
  JSON.stringify(
    results.map(({ filePath, messages }) => ({
      filePath,
      messages: messages.map(({ ruleId, severity, message, line, fatal }) => ({
        ruleId,
        severity,
        message,
        line,
        fatal: fatal === true,
      })),
    })),
  );
