import fs from 'node:fs';
import path from 'node:path';

/**
 * Resolve the installation-owned launcher used to finish a source update.
 *
 * Windows PM installs can contain two launchers during an update. A locked
 * executable can remain beside the current command file. Select the command
 * file explicitly because extensionless lookup would choose the executable.
 *
 * @param {string} root
 * @param {NodeJS.ProcessEnv | Record<string, string>} env
 * @param {NodeJS.Platform} platform
 * @returns {{launcher: string, command: string, args: string[], windowsVerbatimArguments: boolean}}
 */
export function sourceRuntimeSettleCommand(root, env, platform = process.platform) {
  const local = path.join(root, '.hermes', 'bin');
  const candidates = platform === 'win32'
    ? [path.join(local, 'hermes.cmd'), path.join(local, 'hermes.exe'),
      path.join(root, 'venv', 'Scripts', 'hermes.exe')]
    : [path.join(local, 'hermes'), path.join(root, 'venv', 'bin', 'hermes')];
  const launcher = candidates.find(candidate => fs.existsSync(candidate));
  if (!launcher) throw new Error(`No source launcher available to settle ${root}`);

  if (platform !== 'win32' || path.extname(launcher).toLowerCase() !== '.cmd') {
    return { launcher, command: launcher, args: ['status'], windowsVerbatimArguments: false };
  }
  void env;
  // PM's fallback command launcher embeds the Python bootstrap in a base64
  // `-c` argument. Running that .cmd through cmd.exe constrains the already
  // long command to 8191 characters; a source update's clean-interpreter
  // relaunch then exceeds CreateProcess' limit as well. Use the launcher's
  // selected Python to run the source bootstrap by file instead. This drives
  // the same lazy source-update completion without nesting either command.
  const commandFile = fs.readFileSync(launcher, 'utf8');
  const generated = commandFile.match(/^\s*@?"([^"\r\n]+)"\s+-I(?:\s|$)/m);
  if (!generated) throw new Error(`Unrecognized source command launcher: ${launcher}`);
  const command = generated[1];
  const bootstrap = path.join(root, 'hermes_bootstrap.py');
  if (!fs.existsSync(command)) throw new Error(`Source launcher Python does not exist: ${command}`);
  if (!fs.existsSync(bootstrap)) throw new Error(`Source bootstrap does not exist: ${bootstrap}`);
  const code = `import runpy, sys; sys.path.insert(0, ${JSON.stringify(root)}); sys.argv = [${JSON.stringify(bootstrap)}, 'status']; runpy.run_path(${JSON.stringify(bootstrap)}, run_name='__main__')`;
  return { launcher, command, args: ['-I', '-B', '-c', code], windowsVerbatimArguments: false };
}
