FROM alpine:3.20
RUN apk add --no-cache bash curl jq
COPY loadgen.sh /loadgen.sh
RUN chmod +x /loadgen.sh
ENTRYPOINT ["/loadgen.sh"]
