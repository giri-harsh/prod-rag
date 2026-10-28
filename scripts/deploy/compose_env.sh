# Sourced by the deploy scripts after `cd $APP_DIR`. Sets DC (the compose command with the right overlay files) and DEPLOY_PROFILE.
#   DEPLOY_PROFILE=micro in .env (or the environment) adds docker-compose.micro.yml for ~1 GB VMs; empty = full prod profile.
_cenv() { grep -E "^[[:space:]]*$1[[:space:]]*=" .env 2>/dev/null | tail -1 | sed -E "s/^[^=]*=//; s/[[:space:]]+#.*$//; s/^[[:space:]\"']+//; s/[[:space:]\"']+$//"; }
DEPLOY_PROFILE="${DEPLOY_PROFILE:-$(_cenv DEPLOY_PROFILE)}"
COMPOSE_FILES="-f docker-compose.yml -f docker-compose.prod.yml"
if [ "$DEPLOY_PROFILE" = micro ]; then COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.micro.yml"; fi
DC="sudo docker compose $COMPOSE_FILES"
export DEPLOY_PROFILE
