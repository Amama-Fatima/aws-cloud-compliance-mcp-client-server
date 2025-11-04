FROM eclipse-temurin:21-jdk-alpine AS builder

WORKDIR /build

COPY pom.xml .
COPY src ./src

RUN apk add --no-cache maven
RUN mvn clean package -DskipTests

# Runtime stage with Node.js for the wrapper
FROM node:20-alpine

WORKDIR /app

# Install Java runtime
RUN apk add --no-cache openjdk21-jre

# Copy Java artifact
COPY --from=builder /build/target/cloud-compliance-mcp-0.1.0.jar /app/cloud-compliance-mcp.jar

# Copy Node.js wrapper
COPY server.js package.json package-lock.json ./

# Install Node dependencies
RUN npm ci --only=production

ENV AWS_REGION="ap-south-1"
ENV PORT=10000

EXPOSE 10000

CMD ["node", "server.js"]