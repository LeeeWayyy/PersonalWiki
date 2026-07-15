export function authHeaders(token = localStorage.getItem('backendToken') || '') {
  return token ? { 'X-Auth-Token': token } : {};
}

export function api(path, { json, headers, ...options } = {}) {
  const hasJson = json !== undefined;
  return fetch(path, {
    ...options,
    headers: { ...authHeaders(), ...(hasJson ? { 'Content-Type': 'application/json' } : {}), ...headers },
    ...(hasJson ? { body: JSON.stringify(json) } : {}),
  });
}

export async function streamJob(jobId, append) {
  const res = await api('/jobs/' + jobId + '/events');
  if (!res.ok) throw new Error(await res.text());
  if (!res.body) throw new Error('stream unavailable');
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  let terminalStatus = '';

  function handle(raw) {
    let event = 'message';
    const data = [];
    raw.split(/\r?\n/).forEach((line) => {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) data.push(line.slice(5).trimStart());
    });
    const text = data.join('\n');
    if (!text) return;
    if (event === 'done') {
      terminalStatus = text;
      append('done: ' + text);
    } else {
      append(text);
    }
  }

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf('\n\n')) !== -1) {
      handle(buf.slice(0, idx));
      buf = buf.slice(idx + 2);
    }
  }
  buf += decoder.decode();
  if (buf.trim()) handle(buf);
  if (!terminalStatus) throw new Error('job stream closed before terminal status');
  if (terminalStatus !== 'done') throw new Error(`job ${terminalStatus}`);
}
