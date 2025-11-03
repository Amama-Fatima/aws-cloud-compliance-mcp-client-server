# Stage 1: Build
FROM eclipse-temurin:21-jdk-alpine AS builder

WORKDIR /build

# Copy Maven files
COPY pom.xml .
COPY src ./src

# Install Maven
RUN apk add --no-cache maven

# Build the application
RUN mvn clean package -DskipTests

# Stage 2: Runtime
FROM eclipse-temurin:21-jre-alpine

WORKDIR /app

# Copy only the built JAR from builder stage
COPY --from=builder /build/target/cloud-compliance-mcp-0.1.0.jar /app/cloud-compliance-mcp.jar

# Set AWS environment variables
ENV AWS_ACCESS_KEY_ID=""
ENV AWS_SECRET_ACCESS_KEY=""
ENV AWS_REGION="ap-south-1"

# Run the MCP server
ENTRYPOINT ["java", "-jar", "cloud-compliance-mcp.jar"]