// Dependabot writes sentence-case subjects ("ci: Bump x from 1.0.0 to 1.1.0")
// that subject-case rejects, and the setting cannot be changed. Ignore only that
// exact subject shape so the rule still applies in full to every other commit.
const dependabotBump = /^ci(\(deps\))?: Bump \S+ from \S+ to \S+$/;

export default {
  extends: ['@commitlint/config-conventional'],
  ignores: [(msg) => dependabotBump.test(msg.split('\n')[0])],
};
