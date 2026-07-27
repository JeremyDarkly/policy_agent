FROM node:24-alpine

WORKDIR /app

# Copy package files from app/ui
COPY app/ui/package*.json ./

# Install dependencies (uses package-lock.json)
RUN npm ci

# Copy rest of source code
COPY app/ui/ .

# Expose port for dev server
EXPOSE 5173

# Run dev server with host binding to allow external connections
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
