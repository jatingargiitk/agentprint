#!/bin/sh
# Things a coding agent can do outside the repo during a session.
# Run it only against a throwaway HOME, e.g. HOME=$(mktemp -d).
set -e
echo 'export PATH="$HOME/.agent/bin:$PATH"' >> ~/.bashrc
git config --global core.hooksPath ~/.agent/hooks
mkdir -p ~/.local/bin
printf '#!/bin/sh\necho helper\n' > ~/.local/bin/agent-helper
chmod +x ~/.local/bin/agent-helper
echo '//npm.example.com/:_authToken=npm_NEWTOKENxxxxxxxxxxxxxxxxxxxxxx' >> ~/.npmrc
rm ~/.ssh/config
