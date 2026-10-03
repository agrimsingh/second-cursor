import { constants } from 'node:fs';
import { open, mkdir, rename, unlink } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { hostname } from 'node:os';
import { randomUUID } from 'node:crypto';

export class WorkflowCheckpointError extends Error {
  constructor(message, code = 'INVALID_CHECKPOINT', options) {
    super(message, options); this.name = 'WorkflowCheckpointError'; this.code = code;
  }
}
const fail = (message, code) => { throw new WorkflowCheckpointError(message, code); };

/** Explicitly opt in to saving task content. Keep this file under your own retention policy. */
export function fileCheckpointStore(path, { maxBytes = 16 * 1024 * 1024 } = {}) {
  if (typeof path !== 'string' || !path || !Number.isSafeInteger(maxBytes) || maxBytes < 1)
    fail('A checkpoint path and positive maxBytes are required');
  path = resolve(path);
  const lockPath = `${path}.lock`, recoveryPath = `${lockPath}.recover`;
  async function readSecure(file, limit = maxBytes) {
    let handle;
    try {
      handle = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW);
      const info = await handle.stat();
      if (!info.isFile() || (info.mode & 0o077) !== 0 || (process.getuid && info.uid !== process.getuid()))
        fail('Checkpoint files must be regular, owned by this user, and accessible only to this user');
      if (info.size > limit) fail('Checkpoint exceeds the configured size limit');
      // Read a bounded buffer even if another writer grows the file after fstat.
      const buffer = Buffer.alloc(limit + 1);
      let length = 0;
      while (length < buffer.length) {
        const read = await handle.read(buffer, length, buffer.length - length, length);
        if (!read.bytesRead) break;
        length += read.bytesRead;
      }
      if (length > limit) fail('Checkpoint exceeds the configured size limit');
      try { return JSON.parse(buffer.subarray(0, length).toString('utf8')); }
      catch (cause) { throw new WorkflowCheckpointError('Checkpoint contains invalid JSON', 'INVALID_CHECKPOINT', { cause }); }
    } catch (cause) {
      if (cause.code === 'ENOENT') return null;
      if (cause instanceof WorkflowCheckpointError) throw cause;
      throw new WorkflowCheckpointError('Cannot read checkpoint safely', 'INVALID_CHECKPOINT', { cause });
    } finally { await handle?.close(); }
  }
  async function createLock(owner) {
    const handle = await open(lockPath, 'wx', 0o600);
    try { await handle.writeFile(JSON.stringify(owner)); await handle.sync(); }
    finally { await handle.close(); }
  }
  function deadLocalOwner(owner) {
    if (!owner || owner.hostname !== hostname() || !Number.isSafeInteger(owner.pid) || owner.pid <= 0 || typeof owner.id !== 'string') return false;
    try { process.kill(owner.pid, 0); return false; }
    catch (error) { return error.code === 'ESRCH'; }
  }
  return {
    async acquire() {
      await mkdir(dirname(path), { recursive: true, mode: 0o700 });
      const owner = { id: randomUUID(), pid: process.pid, hostname: hostname() };
      try { await createLock(owner); }
      catch (error) {
        if (error.code !== 'EEXIST') throw error;
        const prior = await readSecure(lockPath, 4096);
        if (!deadLocalOwner(prior)) fail('Checkpoint is locked by another runner; no actions were dispatched', 'CHECKPOINT_LOCKED');
        // Serialize stale-owner recovery. A crash inside this tiny recovery section fails closed;
        // an operator may remove the .recover file after checking that no runner is alive.
        let recovery;
        try { recovery = await open(recoveryPath, 'wx', 0o600); }
        catch (cause) { throw new WorkflowCheckpointError('Checkpoint lock recovery is already in progress', 'CHECKPOINT_LOCKED', { cause }); }
        try {
          const current = await readSecure(lockPath, 4096);
          if (current?.id !== prior.id || !deadLocalOwner(current)) fail('Checkpoint ownership changed during recovery', 'CHECKPOINT_LOCKED');
          await unlink(lockPath);
          try { await createLock(owner); }
          catch (cause) { throw new WorkflowCheckpointError('Another runner acquired the checkpoint', 'CHECKPOINT_LOCKED', { cause }); }
        } finally { await recovery.close(); await unlink(recoveryPath); }
      }
      let released = false;
      async function requireOwner() {
        if (released || (await readSecure(lockPath, 4096))?.id !== owner.id)
          fail('Checkpoint lease is no longer owned by this runner', 'CHECKPOINT_LOCKED');
      }
      return {
        async load() { await requireOwner(); return readSecure(path); },
        async save(value) {
          await requireOwner();
          // Validate any existing destination before replacing it, including a symlink.
          await readSecure(path);
          const bytes = JSON.stringify(value);
          if (Buffer.byteLength(bytes, 'utf8') > maxBytes) fail('Checkpoint exceeds the configured size limit');
          const temporary = `${path}.${owner.id}.${randomUUID()}.tmp`;
          let handle;
          try {
            handle = await open(temporary, 'wx', 0o600);
            await handle.writeFile(bytes); await handle.sync(); await handle.close(); handle = null;
            await requireOwner(); await rename(temporary, path);
            const directory = await open(dirname(path), 'r');
            try { await directory.sync(); } finally { await directory.close(); }
          } finally {
            await handle?.close();
            await unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; });
          }
        },
        async release() {
          if (released) return;
          await requireOwner(); released = true; await unlink(lockPath);
        },
      };
    },
  };
}
