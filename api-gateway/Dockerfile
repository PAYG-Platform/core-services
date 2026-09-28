# Base image: Official lightweight Nginx Alpine image
FROM nginx:alpine

# Copy template into /etc/nginx/templates/ so Nginx auto-substitutes environment variables on boot
COPY default.conf.template /etc/nginx/templates/default.conf.template

# Fallback default configuration if envsubst is not used
COPY default.conf.template /etc/nginx/conf.d/default.conf

# Expose Nginx default HTTP port
EXPOSE 80