# Press Node Agent

Press Node Agent is an FastAPI based reverse proxy which bridges communication between local agents, control-plane and external clients.

The main purpose of this component is to stay in between the components and take responsibiliy of the routing and authentication of incoming requests, so that no agent needs to avoid duplication of the same auth logic.


## Documentation
- [Node Agent Overview](docs/1-overview.md)
- [Setup Guide](docs/2-setup.md)
- [Request Proxying](docs/3-proxying.md)
- [Authorization and Write Agent](docs/4-write-agent.md)

## License
The project is licensed under AGPL-3.0 License. See [LICENSE](./license.txt) for more details.