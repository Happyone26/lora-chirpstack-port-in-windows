FROM node:22-alpine

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY index.js ./

# UID 1002 matches the host user so files written to the CSV volume mount
# aren't owned by root (avoids the sudo-chown friction we hit elsewhere).
RUN adduser -D -u 1002 appuser && mkdir -p /data && chown appuser:appuser /data
USER appuser

EXPOSE 9200
CMD ["node", "index.js"]
